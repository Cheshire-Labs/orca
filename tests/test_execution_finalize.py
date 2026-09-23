"""T6 Execution state machine: ACCEPTING -> DRAINING -> terminal.

Validates:
- A fresh active execution starts in ACCEPTING.
- runtime.close_execution(execution_id) transitions the execution to
  DRAINING; the execution stays in the active-executions index so
  subsequent JOIN_EXISTING submits can distinguish "draining" from
  "unknown execution".
- STANDALONE grouped submits against the drained execution boot a fresh
  execution (default batch_mode). JOIN_EXISTING submits are rejected
  with RuntimeError.
- close_execution raises KeyError on an unknown execution_id.
"""
import asyncio
from uuid import uuid4

import pytest

from tests.test_helpers import execution_outcome

from orca.runtime.execution import ExecutionPhase
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.submission import BatchMode
from orca.runtime.system_runtime import SystemRuntime
from orca.runtime.run_modes import WorkflowRunMode

from tests.test_multi_lineage_demo import _build_multi_lineage_system


def _group() -> LabwareGroup:
    return LabwareGroup(
        id=str(uuid4()),
        members=(LabwareGroupMember(thread_template_name="sample"),),
    )


class TestExecutionStateMachine:

    @pytest.mark.asyncio
    async def test_execution_starts_accepting(self) -> None:
        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            submission = await runtime.submit(workflow, groups=[_group()], mode=WorkflowRunMode.PURE_SIM)
            execution = runtime._executions[submission.execution_id]
            assert execution.phase is ExecutionPhase.ACCEPTING
            await execution_outcome(runtime, submission, timeout=30.0)
            assert execution.phase is ExecutionPhase.COMPLETED
        finally:
            await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_close_execution_transitions_to_draining(self) -> None:
        """close_execution transitions ACCEPTING -> DRAINING. The entry stays
        in the active-executions index so JOIN_EXISTING submits against this
        execution can be distinguished from "no such execution"."""
        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            submission = await runtime.submit(workflow, groups=[_group()], mode=WorkflowRunMode.PURE_SIM)
            execution = runtime._executions[submission.execution_id]
            result = runtime.close_execution(execution.id)
            assert result is ExecutionPhase.DRAINING
            assert execution.phase is ExecutionPhase.DRAINING
            assert runtime._active_executions.get(workflow.name) is execution
        finally:
            await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_standalone_submit_after_close_boots_fresh_execution(self) -> None:
        """Default batch_mode is STANDALONE. After close, a next submit
        starts a fresh execution that replaces the draining one in the
        active-executions index."""
        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            sub1 = await runtime.submit(workflow, groups=[_group()], mode=WorkflowRunMode.PURE_SIM)
            runtime.close_execution(sub1.execution_id)
            sub2 = await runtime.submit(workflow, groups=[_group()], mode=WorkflowRunMode.PURE_SIM)
            assert sub1.execution_id != sub2.execution_id
            assert runtime._executions[sub2.execution_id].phase is ExecutionPhase.ACCEPTING
            assert runtime._active_executions[workflow.name].id == sub2.execution_id
        finally:
            await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_join_existing_submit_after_close_raises(self) -> None:
        """JOIN_EXISTING targeting a DRAINING execution is rejected with
        RuntimeError; there's no ACCEPTING execution for it to join."""
        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            sub1 = await runtime.submit(workflow, groups=[_group()], mode=WorkflowRunMode.PURE_SIM)
            runtime.close_execution(sub1.execution_id)
            with pytest.raises(RuntimeError, match="DRAINING"):
                await runtime.submit(
                    workflow, groups=[_group()],
                    batch_mode=BatchMode.JOIN_EXISTING,
                    mode=WorkflowRunMode.PURE_SIM,
                )
        finally:
            await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_join_existing_submit_against_stopping_raises(self) -> None:
        """JOIN_EXISTING against a STOPPING execution must refuse with the same
        RuntimeError class that DRAINING uses. Pre-fix, STOPPING fell
        through to the else branch and silently booted a fresh execution
        instead of signalling to the operator that the join target is gone.
        """
        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            sub1 = await runtime.submit(workflow, groups=[_group()], mode=WorkflowRunMode.PURE_SIM)
            await runtime.abort_execution(sub1.execution_id)
            assert runtime._executions[sub1.execution_id].phase is ExecutionPhase.STOPPING
            with pytest.raises(RuntimeError, match="STOPPING"):
                await runtime.submit(
                    workflow, groups=[_group()],
                    batch_mode=BatchMode.JOIN_EXISTING,
                    mode=WorkflowRunMode.PURE_SIM,
                )
        finally:
            await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_close_unknown_execution_raises(self) -> None:
        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            with pytest.raises(KeyError, match="does-not-exist"):
                runtime.close_execution("does-not-exist")
        finally:
            await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self) -> None:
        """Calling close on an already-DRAINING execution returns DRAINING
        without mutating anything, matching the docstring. Prior impl raised
        KeyError on the second call because finalize_execution deleted the
        active-executions entry on the first call."""
        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            sub = await runtime.submit(workflow, groups=[_group()], mode=WorkflowRunMode.PURE_SIM)
            runtime.close_execution(sub.execution_id)
            # Second call is a no-op (phase unchanged).
            result = runtime.close_execution(sub.execution_id)
            assert result is ExecutionPhase.DRAINING
        finally:
            await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_close_works_on_groupless_execution(self) -> None:
        """Groupless submissions create their own execution but never
        entered the _active_executions index under the prior impl, so
        closing them via the old workflow_name-keyed finalize_execution
        raised KeyError. Under the execution_id-keyed close this just
        works."""
        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            sub = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
            phase = runtime.close_execution(sub.execution_id)
            assert phase is ExecutionPhase.DRAINING
        finally:
            await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_close_affects_only_targeted_execution(self) -> None:
        """Two separate executions for two different workflows: closing one
        must not touch the other."""
        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            sub_a = await runtime.submit(workflow, groups=[_group()], mode=WorkflowRunMode.PURE_SIM)
            # Close A, then verify a fresh STANDALONE submission for the same
            # workflow starts a distinct execution unaffected by A's DRAINING
            # phase.
            runtime.close_execution(sub_a.execution_id)
            sub_b = await runtime.submit(workflow, groups=[_group()], mode=WorkflowRunMode.PURE_SIM)

            exec_a = runtime._executions[sub_a.execution_id]
            exec_b = runtime._executions[sub_b.execution_id]
            assert exec_a.phase is ExecutionPhase.DRAINING
            assert exec_b.phase is ExecutionPhase.ACCEPTING
            # Closing A again doesn't perturb B's phase.
            runtime.close_execution(sub_a.execution_id)
            assert exec_b.phase is ExecutionPhase.ACCEPTING
        finally:
            await runtime.shutdown()
