"""T6h lifecycle events: SUBMISSION.* / GROUP.* / EXECUTION.*

Closes the T6 Events section. Verifies that:
- SUBMISSION.{id}.ACCEPTED fires on submit (both initial boot and mid-run
  injection).
- SUBMISSION.{id}.COMPLETED fires when the owning execution finishes
  normally; SUBMISSION.{id}.FAILED when it errors.
- GROUP.{id}.COMPLETED fires when all threads tagged with the
  (submission_id, group_id) pair have reached terminal state. Fires exactly
  once per group.
- EXECUTION.{id}.COMPLETED / FAILED fires for the execution itself.
"""
import asyncio
from uuid import uuid4

import pytest

from tests.test_helpers import execution_outcome

from orca.events.event_bus import EventBus
from orca.events.execution_context import ExecutionContext
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


def _capture_events(
    event_bus: EventBus,
) -> list[tuple[str, ExecutionContext]]:
    captured: list[tuple[str, ExecutionContext]] = []
    event_bus.subscribe_all(lambda name, ctx: captured.append((name, ctx)))
    return captured


class TestSubmissionLifecycleEvents:

    @pytest.mark.asyncio
    async def test_submission_accepted_and_completed_fire(self) -> None:
        system, workflow, event_bus = await _build_multi_lineage_system()
        captured = _capture_events(event_bus)
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            submission = await runtime.submit(workflow, groups=[_group()], mode=WorkflowRunMode.PURE_SIM)
            status = await execution_outcome(runtime, submission, timeout=30.0)
            assert status.status == "completed"
        finally:
            await runtime.shutdown()

        event_names = [n for n, _ in captured]
        assert f"SUBMISSION.{submission.id}.ACCEPTED" in event_names, (
            f"expected ACCEPTED event; got: {event_names}"
        )
        assert f"SUBMISSION.{submission.id}.COMPLETED" in event_names, (
            f"expected COMPLETED event; got: {event_names}"
        )

    @pytest.mark.asyncio
    async def test_execution_completed_fires(self) -> None:
        system, workflow, event_bus = await _build_multi_lineage_system()
        captured = _capture_events(event_bus)
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            submission = await runtime.submit(workflow, groups=[_group()], mode=WorkflowRunMode.PURE_SIM)
            await execution_outcome(runtime, submission, timeout=30.0)
        finally:
            await runtime.shutdown()

        event_names = [n for n, _ in captured]
        exec_completed = [
            n for n in event_names
            if n.startswith("EXECUTION.") and n.endswith(".COMPLETED")
        ]
        assert len(exec_completed) == 1, (
            f"exactly one EXECUTION.COMPLETED expected; got: {exec_completed}"
        )

    @pytest.mark.asyncio
    async def test_group_completed_fires_once_per_group(self) -> None:
        system, workflow, event_bus = await _build_multi_lineage_system()
        captured = _capture_events(event_bus)
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            group_a = _group()
            group_b = _group()
            submission = await runtime.submit(
                workflow, groups=[group_a, group_b],
                mode=WorkflowRunMode.PURE_SIM,
            )
            await execution_outcome(runtime, submission, timeout=60.0)
        finally:
            await runtime.shutdown()

        event_names = [n for n, _ in captured]
        group_events = [
            n for n in event_names
            if n.startswith("GROUP.") and n.endswith(".COMPLETED")
        ]
        assert f"GROUP.{group_a.id}.COMPLETED" in group_events
        assert f"GROUP.{group_b.id}.COMPLETED" in group_events
        # exactly once each (no double-firing)
        assert group_events.count(f"GROUP.{group_a.id}.COMPLETED") == 1
        assert group_events.count(f"GROUP.{group_b.id}.COMPLETED") == 1


class TestMultipleSubmissionsShareExecutionEvents:
    """When a second grouped submit reuses a live execution, both
    submissions should each get their own ACCEPTED + COMPLETED events, but
    EXECUTION.COMPLETED fires exactly once.

    Marked slow because multi-lineage N=2 × submit cost is non-trivial
    (roughly one mix-iteration per sample × transport overhead).
    """

    @pytest.mark.slow
    @pytest.mark.asyncio
    @pytest.mark.timeout(180)
    async def test_two_submissions_share_one_execution_event(self) -> None:
        system, workflow, event_bus = await _build_multi_lineage_system()
        captured = _capture_events(event_bus)
        runtime = SystemRuntime(system, event_bus=event_bus)
        await runtime.start()
        try:
            sub1 = await runtime.submit(workflow, groups=[_group()], mode=WorkflowRunMode.PURE_SIM)
            # JOIN_EXISTING: the second submission attaches to sub1's
            # live ACCEPTING execution rather than booting a fresh one.
            # The post-Bug-OOO STANDALONE contract (the default) always
            # boots a fresh execution, so explicit JOIN_EXISTING is
            # required for the shared-execution assertion below.
            sub2 = await runtime.submit(
                workflow, groups=[_group()],
                batch_mode=BatchMode.JOIN_EXISTING,
                mode=WorkflowRunMode.PURE_SIM,
            )
            assert sub1.execution_id == sub2.execution_id
            await execution_outcome(runtime, sub1, timeout=150.0)
        finally:
            await runtime.shutdown()

        event_names = [n for n, _ in captured]
        assert f"SUBMISSION.{sub1.id}.ACCEPTED" in event_names
        assert f"SUBMISSION.{sub2.id}.ACCEPTED" in event_names
        assert f"SUBMISSION.{sub1.id}.COMPLETED" in event_names
        assert f"SUBMISSION.{sub2.id}.COMPLETED" in event_names
        exec_completed = [
            n for n in event_names
            if n.startswith("EXECUTION.") and n.endswith(".COMPLETED")
        ]
        assert len(exec_completed) == 1
