"""Tests for ActionContext.manual_step() -- operator pause/confirm flow.

manual_step() logs an instruction, emits OPERATOR.INSTRUCTION with a
unique step_id, and waits for OPERATOR.CONFIRM.{step_id}. Each call
gets its own confirmation channel so concurrent manual steps don't
interfere.
"""

import asyncio
import logging

import pytest

from orca.events.event_channel import EventChannelRegistry
from orca.variables.variable_store import NullVariableResolver
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.device_handle import ActionRequest


def _make_ctx(
    event_registry: EventChannelRegistry,
) -> ActionContext:
    queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
    return ActionContext(
        device_name="test_device",
        action_queue=queue,
        assigned_labware={},
        variable_store=NullVariableResolver(),
        execution_id="test-exec-1",
        event_channel_registry=event_registry,
    )


class TestManualStep:

    @pytest.mark.asyncio
    async def test_manual_step_emits_and_waits(self) -> None:
        """manual_step() emits OPERATOR.INSTRUCTION then waits for confirm."""
        registry = EventChannelRegistry()
        ctx = _make_ctx(registry)

        instruction_channel = registry.get_or_create("OPERATOR.INSTRUCTION")

        async def run_manual_step() -> None:
            await ctx.manual_step("Remove plate and centrifuge at 300g")

        task = asyncio.create_task(run_manual_step())

        # Wait for the OPERATOR.INSTRUCTION event
        _counter, step_id, data = await instruction_channel.wait(
            seen_counter=0, timeout=2.0,
        )
        assert isinstance(step_id, str)
        assert step_id.startswith("manual_step-")
        assert data["instruction"] == "Remove plate and centrifuge at 300g"
        assert data["step_id"] == step_id

        # Confirm the step
        confirm_channel = registry.get_or_create(f"OPERATOR.CONFIRM.{step_id}")
        await confirm_channel.publish(value="confirmed")

        # manual_step should return
        await asyncio.wait_for(task, timeout=2.0)

    @pytest.mark.asyncio
    async def test_manual_step_records_pending_on_emit(self) -> None:
        """manual_step() registers a PendingManualStep visible while it waits."""
        registry = EventChannelRegistry()
        ctx = _make_ctx(registry)

        instruction_channel = registry.get_or_create("OPERATOR.INSTRUCTION")
        task = asyncio.create_task(ctx.manual_step("Centrifuge plate"))

        _counter, step_id, _data = await instruction_channel.wait(
            seen_counter=0, timeout=2.0,
        )
        assert isinstance(step_id, str)
        pending = registry.list_manual_steps()
        assert [p.step_id for p in pending] == [step_id]
        assert pending[0].instruction == "Centrifuge plate"

        confirm = registry.get_or_create(f"OPERATOR.CONFIRM.{step_id}")
        await confirm.publish(value="confirmed")
        await asyncio.wait_for(task, timeout=2.0)
        assert registry.list_manual_steps() == []

    @pytest.mark.asyncio
    async def test_manual_step_timeout(self) -> None:
        """manual_step() raises TimeoutError when confirm is not received."""
        registry = EventChannelRegistry()
        ctx = _make_ctx(registry)

        with pytest.raises(asyncio.TimeoutError):
            await ctx.manual_step(
                "This will timeout",
                timeout_hours=0.0001,  # 0.36 seconds
            )
        # The finally clause must clear the pending record so a timed-out
        # step does not linger as pending forever.
        assert registry.list_manual_steps() == []

    @pytest.mark.asyncio
    async def test_manual_step_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """manual_step() logs instruction to the orca.operator logger."""
        registry = EventChannelRegistry()
        ctx = _make_ctx(registry)

        instruction_channel = registry.get_or_create("OPERATOR.INSTRUCTION")

        async def run_and_confirm() -> None:
            _counter, step_id, _data = await instruction_channel.wait(
                seen_counter=0, timeout=2.0,
            )
            confirm = registry.get_or_create(f"OPERATOR.CONFIRM.{step_id}")
            await confirm.publish(value="confirmed")

        with caplog.at_level(logging.INFO, logger="orca.operator"):
            confirm_task = asyncio.create_task(run_and_confirm())
            await ctx.manual_step("Inspect wells for contamination")
            await confirm_task

        matching = [r for r in caplog.records if r.name == "orca.operator"]
        assert len(matching) == 1
        record = matching[0]
        assert "ACTION REQUIRED" in record.message
        assert "Inspect wells for contamination" in record.message
        assert "manual_step-" in record.message

    @pytest.mark.asyncio
    async def test_two_concurrent_manual_steps_independent(self) -> None:
        """Two concurrent manual_step() calls get independent confirm channels."""
        registry = EventChannelRegistry()
        ctx_a = _make_ctx(registry)
        ctx_b = _make_ctx(registry)

        instruction_channel = registry.get_or_create("OPERATOR.INSTRUCTION")

        # Start first manual step and capture its step_id before starting the second.
        # EventChannel latch only stores the latest value, so we must read each
        # publish before the next one overwrites it.
        task_a = asyncio.create_task(ctx_a.manual_step("Step A"))
        counter_a, step_id_a, _data_a = await instruction_channel.wait(
            seen_counter=0, timeout=2.0,
        )

        task_b = asyncio.create_task(ctx_b.manual_step("Step B"))
        _counter_b, step_id_b, _data_b = await instruction_channel.wait(
            seen_counter=counter_a, timeout=2.0,
        )

        assert step_id_a != step_id_b

        # Confirm only step A
        confirm_a = registry.get_or_create(f"OPERATOR.CONFIRM.{step_id_a}")
        await confirm_a.publish(value="confirmed")

        # Task A should complete, task B should still be waiting
        await asyncio.wait_for(task_a, timeout=2.0)
        assert not task_b.done()

        # Confirm step B
        confirm_b = registry.get_or_create(f"OPERATOR.CONFIRM.{step_id_b}")
        await confirm_b.publish(value="confirmed")

        await asyncio.wait_for(task_b, timeout=2.0)


class _HoldingCheckpoint:
    """Stands in for the thread's pause machinery inside an action body."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def hold_if_pause_requested(self) -> None:
        self.entered.set()
        await self.release.wait()


class TestManualStepPauseCheckpoint:
    """The checkpoint the thread offers is consulted after the operator confirms."""

    @staticmethod
    def _ctx_with(
        registry: EventChannelRegistry, checkpoint: _HoldingCheckpoint,
    ) -> ActionContext:
        queue: asyncio.Queue[ActionRequest | None] = asyncio.Queue()
        return ActionContext(
            device_name="test_device",
            action_queue=queue,
            assigned_labware={},
            variable_store=NullVariableResolver(),
            execution_id="test-exec-1",
            event_channel_registry=registry,
            pause_checkpoint=checkpoint,
        )

    @pytest.mark.asyncio
    async def test_confirm_enters_the_checkpoint_before_returning(self) -> None:
        registry = EventChannelRegistry()
        checkpoint = _HoldingCheckpoint()
        ctx = self._ctx_with(registry, checkpoint)

        instructions = registry.get_or_create("OPERATOR.INSTRUCTION")
        task = asyncio.create_task(ctx.manual_step("swap the trough"))
        _counter, step_id, _data = await instructions.wait(seen_counter=0, timeout=2.0)
        assert isinstance(step_id, str)

        await registry.get_or_create(f"OPERATOR.CONFIRM.{step_id}").publish(
            value="confirmed",
        )
        await asyncio.wait_for(checkpoint.entered.wait(), timeout=2.0)

        assert not task.done(), "manual_step must not return while the thread is held"

        checkpoint.release.set()
        await asyncio.wait_for(task, timeout=2.0)

    @pytest.mark.asyncio
    async def test_hold_runs_outside_the_confirm_timeout(self) -> None:
        """The step is already confirmed and cleared when the hold begins.

        Which is what puts the hold outside ``timeout_hours``: that bounds the
        wait for the operator to act, and by here they have.
        """
        registry = EventChannelRegistry()
        checkpoint = _HoldingCheckpoint()
        ctx = self._ctx_with(registry, checkpoint)

        instructions = registry.get_or_create("OPERATOR.INSTRUCTION")
        task = asyncio.create_task(
            ctx.manual_step("swap the trough", timeout_hours=4),
        )
        _counter, step_id, _data = await instructions.wait(seen_counter=0, timeout=2.0)
        assert isinstance(step_id, str)

        await registry.get_or_create(f"OPERATOR.CONFIRM.{step_id}").publish(
            value="confirmed",
        )
        await asyncio.wait_for(checkpoint.entered.wait(), timeout=2.0)

        assert registry.list_manual_steps() == []

        checkpoint.release.set()
        await asyncio.wait_for(task, timeout=2.0)

    @pytest.mark.asyncio
    async def test_timed_out_step_never_reaches_the_checkpoint(self) -> None:
        """No confirm, no hold: the timeout still propagates to the action body."""
        registry = EventChannelRegistry()
        checkpoint = _HoldingCheckpoint()
        ctx = self._ctx_with(registry, checkpoint)

        with pytest.raises(asyncio.TimeoutError):
            await ctx.manual_step("nobody answers", timeout_hours=0.0001)

        assert not checkpoint.entered.is_set()
