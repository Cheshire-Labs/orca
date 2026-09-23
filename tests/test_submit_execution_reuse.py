"""Fix 3 regression tests: SystemRuntime.submit() execution reuse semantics.

The T6 plan distinguishes Execution (long-lived run of a workflow) from
Submission (envelope of labware groups handed to the runtime). One execution
can accept multiple submissions; each submission has its own submission_id
but shares the execution_id of the execution it joined.

Current behaviour:
- **Groupless submit** (legacy one-shot): always creates a fresh Execution,
  matching pre-T6 semantics. `test_critical_bugs` relies on this to treat
  each submit as an independent workflow run.
- **Grouped + JOIN_EXISTING**: opt-in execution reuse. The first grouped
  submit for a workflow name creates an Execution and indexes it by name.
  Subsequent grouped submits with ``batch_mode=BatchMode.JOIN_EXISTING``
  while that Execution is alive reuse its execution_id and inject entry
  threads mid-run.
- **Grouped + STANDALONE** (the default): always boots a fresh Execution
  per the wire contract documented on the hosted REST submit surface and
  the MCP ``submissions_submit`` tool. Pre-fix (Bug OOO), STANDALONE
  silently joined an in-flight ACCEPTING execution because the
  ACCEPTING-phase branch in ``submit()`` was unconditional and
  ``batch_mode`` was only inspected on the DRAINING branch -- a real
  customer-demo failure mode. The tests below pin the corrected
  STANDALONE-is-always-fresh contract alongside the JOIN_EXISTING reuse
  golden path.
"""
import asyncio

import pytest

from tests.test_helpers import execution_outcome

from orca.runtime.execution import Execution
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.submission import BatchMode
from orca.runtime.system_runtime import SystemRuntime
from tests.test_multi_lineage_demo import _build_multi_lineage_system


def _group() -> LabwareGroup:
    from uuid import uuid4
    return LabwareGroup(
        id=str(uuid4()),
        members=(LabwareGroupMember(thread_template_name="sample"),),
    )


class TestGrouplessSubmitsAreIndependent:
    """Groupless submits preserve pre-T6 one-shot semantics.

    Guards test_critical_bugs' assumption that two back-to-back submits
    against the same workflow produce two independent executions.
    """

    @pytest.mark.asyncio
    async def test_two_groupless_submits_get_distinct_execution_ids(self) -> None:
        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            sub1 = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
            sub2 = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
            assert sub1.execution_id != sub2.execution_id, (
                "Groupless submits must create independent executions"
            )
        finally:
            await runtime.shutdown()


class TestGroupedSubmitsShareExecution:
    """Grouped + JOIN_EXISTING submits against the same workflow join one
    Execution. STANDALONE always boots fresh (Bug OOO regression)."""

    @pytest.mark.asyncio
    async def test_two_join_existing_submits_share_execution_id(self) -> None:
        """Golden-path JOIN_EXISTING reuse: the second submission sees
        the first's still-ACCEPTING execution and injects into it."""
        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            sub1 = await runtime.submit(
                workflow, groups=[_group()], batch_mode=BatchMode.JOIN_EXISTING,
                mode=WorkflowRunMode.PURE_SIM,
            )
            sub2 = await runtime.submit(
                workflow, groups=[_group()], batch_mode=BatchMode.JOIN_EXISTING,
                mode=WorkflowRunMode.PURE_SIM,
            )
            assert sub1.execution_id == sub2.execution_id, (
                "JOIN_EXISTING grouped submits for the same workflow must "
                "share execution_id"
            )
            assert sub1.id != sub2.id, (
                "Each submission gets its own submission_id even when sharing "
                "an execution"
            )
        finally:
            await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_standalone_grouped_submits_get_fresh_execution_id(
        self,
    ) -> None:
        """Bug OOO regression: STANDALONE + groups must NOT join an
        in-flight ACCEPTING execution of the same workflow. Each
        STANDALONE submission boots its own fresh execution.

        Pre-fix this test would have failed -- the ACCEPTING-phase
        branch joined unconditionally and ``batch_mode`` was only
        consulted when the existing execution was DRAINING.
        """
        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            sub1 = await runtime.submit(
                workflow, groups=[_group()], batch_mode=BatchMode.STANDALONE,
                mode=WorkflowRunMode.PURE_SIM,
            )
            sub2 = await runtime.submit(
                workflow, groups=[_group()], batch_mode=BatchMode.STANDALONE,
                mode=WorkflowRunMode.PURE_SIM,
            )
            assert sub1.execution_id != sub2.execution_id, (
                "STANDALONE grouped submits must each boot a fresh execution; "
                "joining an in-flight ACCEPTING execution would violate the "
                "wire-documented STANDALONE contract"
            )
        finally:
            await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_standalone_after_join_existing_boots_fresh(self) -> None:
        """A JOIN_EXISTING submission that opens an execution does not
        capture later STANDALONE submissions. STANDALONE remains
        independent regardless of what kind of execution is in flight.
        """
        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            sub_join = await runtime.submit(
                workflow, groups=[_group()],
                batch_mode=BatchMode.JOIN_EXISTING,
                mode=WorkflowRunMode.PURE_SIM,
            )
            sub_standalone = await runtime.submit(
                workflow, groups=[_group()],
                batch_mode=BatchMode.STANDALONE,
                mode=WorkflowRunMode.PURE_SIM,
            )
            assert sub_join.execution_id != sub_standalone.execution_id
        finally:
            await runtime.shutdown()

    @pytest.mark.asyncio
    async def test_grouped_then_groupless_do_not_share(self) -> None:
        """A groupless submit always gets a fresh execution, even if a live
        grouped execution exists for the same workflow."""
        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            sub_grouped = await runtime.submit(
                workflow, groups=[_group()],
                batch_mode=BatchMode.JOIN_EXISTING,
                mode=WorkflowRunMode.PURE_SIM,
            )
            sub_groupless = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
            assert sub_grouped.execution_id != sub_groupless.execution_id
        finally:
            await runtime.shutdown()


class TestExecutionIndexClearedOnCompletion:
    """After an execution finishes, subsequent JOIN_EXISTING submits boot
    fresh -- the index for that workflow name no longer points at a live
    target."""

    @pytest.mark.asyncio
    async def test_fresh_execution_after_prior_completion(self) -> None:
        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            sub1 = await runtime.submit(
                workflow, groups=[_group()],
                batch_mode=BatchMode.JOIN_EXISTING,
                mode=WorkflowRunMode.PURE_SIM,
            )
            await execution_outcome(runtime, sub1, timeout=60.0)

            sub2 = await runtime.submit(
                workflow, groups=[_group()],
                batch_mode=BatchMode.JOIN_EXISTING,
                mode=WorkflowRunMode.PURE_SIM,
            )
            assert sub1.execution_id != sub2.execution_id, (
                "Once an execution completes, even a JOIN_EXISTING submit "
                "must boot a fresh execution -- the existing one is no "
                "longer alive to inject into"
            )
        finally:
            await runtime.shutdown()


class TestInjectSubmissionReCheck:
    """_inject_submission guards against the execution finishing between the
    find-or-create decision and the actual inject by re-checking task.done()
    after waiting for workflow_attached.
    """

    @pytest.mark.asyncio
    async def test_inject_raises_when_execution_task_done(self) -> None:
        from orca.runtime.submission import Submission, SubmissionStatus
        from datetime import datetime, timezone
        from uuid import uuid4

        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            # Build a synthetic Execution with a task that is already done.
            async def _instant() -> None:
                return None
            task = asyncio.create_task(_instant())
            await task  # let it finish

            execution = Execution(
                id=str(uuid4()),
                workflow_name=workflow.name,
                workflow=workflow,
                system=system,
                task=task,
            )
            execution.workflow_attached.set()  # pretend it was attached

            submission = Submission(
                id=str(uuid4()),
                execution_id=execution.id,
                workflow_name=workflow.name,
                groups=(_group(),),
                variables={},
                batch_mode=BatchMode.STANDALONE,
                submitted_at=datetime.now(timezone.utc),
                run_mode=WorkflowRunMode.PURE_SIM,
                operator_id=None,
                deployment_profile=None,
                status=SubmissionStatus.ACCEPTED,
            )

            with pytest.raises(RuntimeError, match="already finished"):
                await runtime._inject_submission(
                    execution, workflow, submission, BatchMode.STANDALONE,
                )
        finally:
            await runtime.shutdown()


class TestWorkflowAttachedEventFiresOnce:
    """The workflow_attached Event is set exactly once inside _run_workflow
    after the ExecutingWorkflow is assigned. Ensures injectors see a
    deterministic attachment signal rather than polling.
    """

    @pytest.mark.asyncio
    async def test_workflow_attached_set_after_submit(self) -> None:
        system, workflow, event_bus = await _build_multi_lineage_system()
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            sub = await runtime.submit(workflow, groups=[_group()], mode=WorkflowRunMode.PURE_SIM)
            execution = runtime._executions[sub.execution_id]
            # The task runs on the loop; give it a tick to attach.
            await asyncio.wait_for(execution.workflow_attached.wait(), timeout=10.0)
            assert execution.executing_workflow is not None
            assert execution.workflow_attached.is_set()
        finally:
            await runtime.shutdown()
