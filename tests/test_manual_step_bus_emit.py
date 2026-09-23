"""ctx.manual_step also broadcasts OPERATOR.INSTRUCTION on the event bus.

Before this, a manual step published only to the per-execution EventChannel,
so an operating LLM watching the SystemEventBus never learned a human was
needed. These tests pin that manual_step ALSO emits an OPERATOR.INSTRUCTION
event (carrying the instruction + step_id) that classifies as an
intervention, while keeping the existing EventChannel publish that the
cross-thread confirm relies on.
"""

import asyncio

import pytest

from orca.events.event_channel import EventChannelRegistry
from orca.events.execution_context import (
    ExecutionContext,
    OperatorInstructionContext,
)
from orca.events.intervention import InterventionKind, classify_intervention
from orca.events.runtime_event import RuntimeEvent
from orca.variables.variable_store import NullVariableResolver
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.device_handle import ActionRequest


def _make_ctx(
    registry: EventChannelRegistry,
    captured: list[tuple[str, ExecutionContext]],
) -> ActionContext:
    queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()

    def emitter(event_name: str, context: ExecutionContext) -> None:
        captured.append((event_name, context))

    return ActionContext(
        device_name="test_device",
        action_queue=queue,
        assigned_labware={},
        variable_store=NullVariableResolver(),
        execution_id="exec-1",
        event_channel_registry=registry,
        event_emitter=emitter,
        workflow_name="wf",
        thread_id="t1",
    )


@pytest.mark.asyncio
async def test_manual_step_emits_operator_instruction_on_bus() -> None:
    registry = EventChannelRegistry()
    captured: list[tuple[str, ExecutionContext]] = []
    ctx = _make_ctx(registry, captured)

    instruction_channel = registry.get_or_create("OPERATOR.INSTRUCTION")
    task = asyncio.create_task(ctx.manual_step("Load reagents into slot A1"))

    _counter, step_id, _data = await instruction_channel.wait(
        seen_counter=0, timeout=2.0,
    )

    assert len(captured) == 1
    name, context = captured[0]
    assert name == "OPERATOR.INSTRUCTION"
    assert isinstance(context, OperatorInstructionContext)
    assert context.instruction == "Load reagents into slot A1"
    assert context.step_id == step_id
    assert context.execution_id == "exec-1"
    assert context.workflow_name == "wf"
    assert context.thread_id == "t1"

    event = RuntimeEvent.from_event_bus("OPERATOR.INSTRUCTION", "exec-1", context)
    assert classify_intervention(event) is InterventionKind.MANUAL_STEP

    confirm = registry.get_or_create(f"OPERATOR.CONFIRM.{step_id}")
    await confirm.publish(value="confirmed")
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_manual_step_still_publishes_event_channel() -> None:
    """The EventChannel publish (cross-thread confirm) must survive the
    addition of the bus emit; the pending-step record still appears."""
    registry = EventChannelRegistry()
    captured: list[tuple[str, ExecutionContext]] = []
    ctx = _make_ctx(registry, captured)

    instruction_channel = registry.get_or_create("OPERATOR.INSTRUCTION")
    task = asyncio.create_task(ctx.manual_step("Centrifuge plate"))

    _c, step_id, data = await instruction_channel.wait(seen_counter=0, timeout=2.0)
    assert data["instruction"] == "Centrifuge plate"
    assert [p.step_id for p in registry.list_manual_steps()] == [step_id]

    confirm = registry.get_or_create(f"OPERATOR.CONFIRM.{step_id}")
    await confirm.publish(value="confirmed")
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_manual_step_without_emitter_does_not_crash() -> None:
    """A directly-built ActionContext (no emitter wired) keeps working;
    the bus emit is best-effort, the EventChannel flow is the contract."""
    registry = EventChannelRegistry()
    queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
    ctx = ActionContext(
        device_name="d", action_queue=queue, assigned_labware={},
        variable_store=NullVariableResolver(), execution_id="exec-1",
        event_channel_registry=registry,
    )

    instruction_channel = registry.get_or_create("OPERATOR.INSTRUCTION")
    task = asyncio.create_task(ctx.manual_step("Do a thing"))
    _c, step_id, _d = await instruction_channel.wait(seen_counter=0, timeout=2.0)
    confirm = registry.get_or_create(f"OPERATOR.CONFIRM.{step_id}")
    await confirm.publish(value="confirmed")
    await asyncio.wait_for(task, timeout=2.0)
