"""Runtime-level tests for the operator manual-step surface.

`SystemRuntime.list_pending_manual_steps` and `confirm_manual_step` are
the engine half of the "operator confirms a manual step" feature. These
tests drive a real `SystemRuntime` with injected `Execution` records that
carry a stub workflow exposing a real `EventChannelRegistry`, plus one
end-to-end test where confirm releases a real waiting `ctx.manual_step`.
"""

import asyncio
from typing import cast

import pytest

from orca.events.event_channel import EventChannelRegistry
from orca.runtime.execution import Execution, ExecutionPhase
from orca.variables.variable_store import NullVariableResolver
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.device_handle import ActionRequest
from orca.workflow_models.workflows.executing_workflow import ExecutingWorkflow

from tests.test_thread_mutation import Fixture, _build_system


class _StubWorkflow:
    """Minimal stand-in exposing only `event_channel_registry`."""

    def __init__(self, registry: EventChannelRegistry) -> None:
        self._registry = registry

    @property
    def event_channel_registry(self) -> EventChannelRegistry:
        return self._registry


async def _make_fixture() -> Fixture:
    return await _build_system()


def _inject_execution(
    fixture: Fixture,
    execution_id: str,
    registry: EventChannelRegistry | None,
) -> None:
    """Stuff a fake Execution into the runtime's table.

    `registry=None` models a not-yet-started execution (executing_workflow
    is None); a registry models a running one.
    """
    runtime = fixture.runtime

    async def _noop() -> None:
        return None

    task = asyncio.get_event_loop().create_task(_noop())
    execution = Execution(
        id=execution_id,
        workflow_name=fixture.workflow.name,
        workflow=fixture.workflow,
        system=runtime.system,
        task=task,
        phase=ExecutionPhase.ACCEPTING,
    )
    if registry is not None:
        execution.executing_workflow = cast(ExecutingWorkflow, _StubWorkflow(registry))
    runtime._executions[execution_id] = execution


class TestListPendingManualSteps:
    @pytest.mark.asyncio
    async def test_global_spans_executions(self) -> None:
        fixture = await _make_fixture()
        reg_a = EventChannelRegistry()
        reg_a.record_manual_step("manual_step-aaaa", "centrifuge plate")
        reg_b = EventChannelRegistry()
        reg_b.record_manual_step("manual_step-bbbb", "inspect wells")
        _inject_execution(fixture, "exec-a", reg_a)
        _inject_execution(fixture, "exec-b", reg_b)

        records = fixture.runtime.list_pending_manual_steps(None)

        by_exec = {r.execution_id: r for r in records}
        assert by_exec["exec-a"].step_id == "manual_step-aaaa"
        assert by_exec["exec-b"].step_id == "manual_step-bbbb"
        assert by_exec["exec-a"].instruction == "centrifuge plate"

    @pytest.mark.asyncio
    async def test_scoped_to_one_execution(self) -> None:
        fixture = await _make_fixture()
        reg_a = EventChannelRegistry()
        reg_a.record_manual_step("manual_step-aaaa", "step a")
        reg_b = EventChannelRegistry()
        reg_b.record_manual_step("manual_step-bbbb", "step b")
        _inject_execution(fixture, "exec-a", reg_a)
        _inject_execution(fixture, "exec-b", reg_b)

        records = fixture.runtime.list_pending_manual_steps("exec-a")

        assert [r.step_id for r in records] == ["manual_step-aaaa"]

    @pytest.mark.asyncio
    async def test_unknown_execution_raises_keyerror(self) -> None:
        fixture = await _make_fixture()
        with pytest.raises(KeyError):
            fixture.runtime.list_pending_manual_steps("nope")

    @pytest.mark.asyncio
    async def test_not_started_execution_returns_empty(self) -> None:
        fixture = await _make_fixture()
        _inject_execution(fixture, "exec-a", None)
        assert fixture.runtime.list_pending_manual_steps("exec-a") == []

    @pytest.mark.asyncio
    async def test_two_concurrent_steps_listed_distinct(self) -> None:
        fixture = await _make_fixture()
        reg = EventChannelRegistry()
        reg.record_manual_step("manual_step-1111", "first")
        reg.record_manual_step("manual_step-2222", "second")
        _inject_execution(fixture, "exec-a", reg)

        records = fixture.runtime.list_pending_manual_steps("exec-a")

        assert {r.step_id for r in records} == {"manual_step-1111", "manual_step-2222"}


class TestConfirmManualStep:
    @pytest.mark.asyncio
    async def test_confirm_releases_waiting_manual_step(self) -> None:
        fixture = await _make_fixture()
        registry = EventChannelRegistry()
        _inject_execution(fixture, "exec-a", registry)

        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        ctx = ActionContext(
            device_name="d",
            action_queue=queue,
            assigned_labware={},
            variable_store=NullVariableResolver(),
            execution_id="exec-a",
            event_channel_registry=registry,
        )
        instruction_channel = registry.get_or_create("OPERATOR.INSTRUCTION")
        task = asyncio.create_task(ctx.manual_step("place tubes"))

        _counter, step_id, _data = await instruction_channel.wait(
            seen_counter=0, timeout=2.0,
        )
        assert isinstance(step_id, str)
        assert registry.has_manual_step(step_id)

        await fixture.runtime.confirm_manual_step("exec-a", step_id)
        await asyncio.wait_for(task, timeout=2.0)
        assert not registry.has_manual_step(step_id)

    @pytest.mark.asyncio
    async def test_unknown_execution_raises_keyerror(self) -> None:
        fixture = await _make_fixture()
        with pytest.raises(KeyError):
            await fixture.runtime.confirm_manual_step("nope", "manual_step-aaaa")

    @pytest.mark.asyncio
    async def test_unknown_step_raises_keyerror(self) -> None:
        fixture = await _make_fixture()
        _inject_execution(fixture, "exec-a", EventChannelRegistry())
        with pytest.raises(KeyError):
            await fixture.runtime.confirm_manual_step("exec-a", "manual_step-missing")

    @pytest.mark.asyncio
    async def test_not_started_execution_raises_keyerror(self) -> None:
        fixture = await _make_fixture()
        _inject_execution(fixture, "exec-a", None)
        with pytest.raises(KeyError):
            await fixture.runtime.confirm_manual_step("exec-a", "manual_step-aaaa")

    @pytest.mark.asyncio
    async def test_double_confirm_second_is_not_found(self) -> None:
        """Two confirms in a row, no manual clear between: only the first wins.

        The runtime's synchronous pop claims the entry on the first confirm,
        so a second concurrent confirm sees it gone and is not-found. This
        exercises the real concurrency window (no workflow-side clear).
        """
        fixture = await _make_fixture()
        registry = EventChannelRegistry()
        registry.record_manual_step("manual_step-aaaa", "x")
        _inject_execution(fixture, "exec-a", registry)

        await fixture.runtime.confirm_manual_step("exec-a", "manual_step-aaaa")
        with pytest.raises(KeyError):
            await fixture.runtime.confirm_manual_step("exec-a", "manual_step-aaaa")

    @pytest.mark.asyncio
    async def test_confirm_removes_pending_immediately(self) -> None:
        """No phantom pending: confirm pops the entry before the action's finally."""
        fixture = await _make_fixture()
        registry = EventChannelRegistry()
        registry.record_manual_step("manual_step-aaaa", "x")
        _inject_execution(fixture, "exec-a", registry)

        await fixture.runtime.confirm_manual_step("exec-a", "manual_step-aaaa")

        assert fixture.runtime.list_pending_manual_steps("exec-a") == []

    @pytest.mark.asyncio
    async def test_confirm_after_timeout_raises_keyerror_no_hang(self) -> None:
        """A timed-out manual_step clears its pending record; confirm is not-found."""
        fixture = await _make_fixture()
        registry = EventChannelRegistry()
        _inject_execution(fixture, "exec-a", registry)

        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        ctx = ActionContext(
            device_name="d",
            action_queue=queue,
            assigned_labware={},
            variable_store=NullVariableResolver(),
            execution_id="exec-a",
            event_channel_registry=registry,
        )
        instruction_channel = registry.get_or_create("OPERATOR.INSTRUCTION")
        task = asyncio.create_task(ctx.manual_step("wait", timeout_hours=0.0001))
        _counter, step_id, _data = await instruction_channel.wait(
            seen_counter=0, timeout=2.0,
        )
        assert isinstance(step_id, str)

        with pytest.raises(asyncio.TimeoutError):
            await task
        assert not registry.has_manual_step(step_id)
        with pytest.raises(KeyError):
            await fixture.runtime.confirm_manual_step("exec-a", step_id)
