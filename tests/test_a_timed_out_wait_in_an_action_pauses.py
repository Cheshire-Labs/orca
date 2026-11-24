"""A wait inside an action that runs out of time pauses for the operator, the same way a failed device call does.

RETRY waits again and re-runs nothing else in the action; the manual step stays pending while paused, so a
confirm that lands during the pause is seen by the retry. Any other decision ends the wait as an action-level
decision.
"""
import asyncio

import pytest

from orca.events.event_channel import EventChannelRegistry
from orca.variables.variable_store import NullVariableResolver
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.actions.operation_recovery import OperationDecisionSignal, device_op_recovery_handler
from orca.workflow_models.device_handle import ActionRequest
from orca.workflow_models.status_enums import PauseSite, RecoveryDecision


def _ctx(registry: EventChannelRegistry) -> ActionContext:
    queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
    return ActionContext(
        device_name="test_device", action_queue=queue, assigned_labware={},
        variable_store=NullVariableResolver(), execution_id="test-exec-1", event_channel_registry=registry,
    )


@pytest.mark.asyncio
async def test_a_timed_out_manual_step_pauses_and_retry_waits_for_the_confirm_again() -> None:
    registry = EventChannelRegistry()
    ctx = _ctx(registry)
    sites: list[PauseSite] = []

    async def operator(error: Exception, label: str, site: PauseSite) -> RecoveryDecision:
        sites.append(site)
        [pending] = registry.list_manual_steps()
        await registry.get_or_create(f"OPERATOR.CONFIRM.{pending.step_id}").publish(value="confirmed")
        return RecoveryDecision.RETRY

    token = device_op_recovery_handler.set(operator)
    try:
        await asyncio.wait_for(ctx.manual_step("swap the trough", timeout_hours=0.00001), timeout=5.0)
    finally:
        device_op_recovery_handler.reset(token)

    assert sites == [PauseSite.ACTION_WAIT]
    assert registry.list_manual_steps() == []


@pytest.mark.asyncio
async def test_a_timed_out_manual_step_pauses_with_the_instruction_not_the_channel_name() -> None:
    registry = EventChannelRegistry()
    ctx = _ctx(registry)
    labels: list[str] = []

    async def operator(error: Exception, label: str, site: PauseSite) -> RecoveryDecision:
        labels.append(label)
        [pending] = registry.list_manual_steps()
        await registry.get_or_create(f"OPERATOR.CONFIRM.{pending.step_id}").publish(value="confirmed")
        return RecoveryDecision.RETRY

    token = device_op_recovery_handler.set(operator)
    try:
        await asyncio.wait_for(ctx.manual_step("swap the trough", timeout_hours=0.00001), timeout=5.0)
    finally:
        device_op_recovery_handler.reset(token)

    [label] = labels
    assert "swap the trough" in label
    assert "OPERATOR.CONFIRM" not in label and "manual_step-" not in label


@pytest.mark.asyncio
async def test_a_timed_out_wait_for_pauses_and_retry_receives_the_event_that_came_later() -> None:
    registry = EventChannelRegistry()
    ctx = _ctx(registry)

    async def operator(error: Exception, label: str, site: PauseSite) -> RecoveryDecision:
        assert site is PauseSite.ACTION_WAIT
        await registry.get_or_create("lid_off").publish(value="done", data={"by": "operator"})
        return RecoveryDecision.RETRY

    token = device_op_recovery_handler.set(operator)
    try:
        value, data = await asyncio.wait_for(ctx.wait_for("lid_off", timeout=0.01), timeout=5.0)
    finally:
        device_op_recovery_handler.reset(token)

    assert (value, data) == ("done", {"by": "operator"})


@pytest.mark.asyncio
async def test_an_action_level_decision_at_a_timed_out_wait_leaves_the_wait() -> None:
    registry = EventChannelRegistry()
    ctx = _ctx(registry)

    async def operator(error: Exception, label: str, site: PauseSite) -> RecoveryDecision:
        return RecoveryDecision.ABORT_ACTION

    token = device_op_recovery_handler.set(operator)
    try:
        with pytest.raises(OperationDecisionSignal) as signal:
            await asyncio.wait_for(ctx.wait_for("never", timeout=0.01), timeout=5.0)
    finally:
        device_op_recovery_handler.reset(token)

    assert signal.value.decision is RecoveryDecision.ABORT_ACTION
