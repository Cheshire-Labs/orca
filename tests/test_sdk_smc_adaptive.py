"""T6f: adaptive submission E2E — mid-run submit + BatchMode routing.

Outcome A (JOIN_EXISTING): sub1 boots the execution, sub2 joins mid-run.
Because final_plate is SHARED_ACROSS_GROUPS + BATCHABLE and both submissions
elect JOIN_EXISTING, the slot key collapses to ``final_plate:*:*``. Both
sub1 and sub2's plate_1 threads feed the SAME final_plate receiver.

Outcome B (both STANDALONE): sub1 boots the execution, sub2 injects mid-run.
STANDALONE keeps submission_id in the slot key even for BATCHABLE templates,
so sub1's key is ``final_plate:*:{sub1_id}`` and sub2's is
``final_plate:*:{sub2_id}`` — TWO independent final_plate receivers.

Both tests use 1-plate submissions (2 plate_1 threads total) to keep sim
runtime manageable while still exercising every code path: mid-run inject,
group-aware slot keying, JOIN_EXISTING vs STANDALONE key composition,
close-firing after all cross-submission feeders terminate.
"""

import asyncio

import pytest

from tests.test_helpers import execution_outcome

from examples.smc_assay.smc_assay_example import build_smc
from orca.plugins import MethodTracker
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.submission import BatchMode
from orca.runtime.system_runtime import SystemRuntime
from orca.runtime.run_modes import WorkflowRunMode


def _plate_group(gid: str) -> LabwareGroup:
    return LabwareGroup(
        id=gid,
        members=(LabwareGroupMember(thread_template_name="plate_1_journey"),),
    )


async def _wait_for_workflow_boot(runtime: SystemRuntime, execution_id: str) -> None:
    """Poll until _run_workflow has attached the ExecutingWorkflow.

    Submitting a second group before this point would fail because the
    inject path reads execution.executing_workflow.
    """
    for _ in range(100):
        execution = runtime._executions[execution_id]
        if execution.executing_workflow is not None:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError("ExecutingWorkflow never attached to execution")


def _thread_counts_by_prefix(tracker: MethodTracker) -> dict[str, int]:
    counts: dict[str, int] = {}
    for tid, name in tracker.thread_names.items():
        prefix = name.rsplit("-", 1)[0]
        counts[prefix] = counts.get(prefix, 0) + 1
    return counts


class TestAdaptiveSubmission:

    @pytest.mark.slow
    @pytest.mark.asyncio
    @pytest.mark.timeout(1000)
    async def test_smc_adaptive_join_existing(self) -> None:
        """Two JOIN_EXISTING submissions collapse onto one final_plate receiver."""
        smc = await build_smc()
        runtime = SystemRuntime(smc.system, event_bus=smc.event_bus)
        tracker = MethodTracker()
        runtime.register_plugin(tracker)
        await runtime.start()

        sub1 = await runtime.submit(
            smc.workflow,
            groups=[_plate_group("grp-1")],
            batch_mode=BatchMode.JOIN_EXISTING,
            mode=WorkflowRunMode.PURE_SIM,
        )
        await _wait_for_workflow_boot(runtime, sub1.execution_id)

        try:
            sub2 = await runtime.submit(
                smc.workflow,
                groups=[_plate_group("grp-2")],
                batch_mode=BatchMode.JOIN_EXISTING,
                mode=WorkflowRunMode.PURE_SIM,
            )

            assert sub1.execution_id == sub2.execution_id, (
                "sub2 should join sub1's execution mid-run"
            )

            await execution_outcome(runtime, sub1, timeout=900.0)
        finally:
            # A failed wait must still tear the runtime down; a leaked runtime's
            # retry storm poisons every test that runs after it in the file.
            await runtime.shutdown()

        counts = _thread_counts_by_prefix(tracker)
        assert counts.get("plate_1") == 2, f"Expected 2 plate_1 threads: {counts}"
        assert counts.get("final_plate") == 1, (
            f"JOIN_EXISTING should collapse to ONE final_plate receiver; got {counts}"
        )

        final_tid = next(
            tid for tid, name in tracker.thread_names.items()
            if name.startswith("final_plate")
        )
        final_methods = tracker.all_completed_snapshots.get(final_tid, [])
        combine_count = sum(1 for m in final_methods if m == "transfer_to_read_plate")
        assert combine_count == 2, (
            f"Shared final_plate receiver should process 2 transfer_to_read_plate contributions; "
            f"got {combine_count}: {final_methods}"
        )
        for terminal in ("centrifuge", "read"):
            assert terminal in final_methods, (
                f"Final plate should have completed {terminal} after close: {final_methods}"
            )

    @pytest.mark.slow
    @pytest.mark.asyncio
    @pytest.mark.timeout(1000)
    async def test_smc_adaptive_both_standalone(self) -> None:
        """Two STANDALONE submissions produce disjoint final_plate receivers
        AND disjoint executions.

        Pre-Bug-OOO-fix this test asserted ``sub1.execution_id ==
        sub2.execution_id`` because the runtime auto-joined STANDALONE
        submissions onto an in-flight ACCEPTING execution and relied on
        the BATCHABLE submission_id keying to keep the receivers
        separate. Post-fix, STANDALONE always boots a fresh execution
        per the wire-documented contract; the disjoint-receiver outcome
        still holds, by construction (each execution has its own
        single submission so the receiver template can't coalesce
        across submissions).
        """
        smc = await build_smc()
        runtime = SystemRuntime(smc.system, event_bus=smc.event_bus)
        tracker = MethodTracker()
        runtime.register_plugin(tracker)
        await runtime.start()

        sub1 = await runtime.submit(
            smc.workflow,
            groups=[_plate_group("grp-1")],
            batch_mode=BatchMode.STANDALONE,
            mode=WorkflowRunMode.PURE_SIM,
        )
        await _wait_for_workflow_boot(runtime, sub1.execution_id)

        try:
            sub2 = await runtime.submit(
                smc.workflow,
                groups=[_plate_group("grp-2")],
                batch_mode=BatchMode.STANDALONE,
                mode=WorkflowRunMode.PURE_SIM,
            )

            assert sub1.execution_id != sub2.execution_id, (
                "STANDALONE submissions must each boot a fresh execution; "
                "joining an in-flight ACCEPTING execution would violate the "
                "wire-documented STANDALONE contract (Bug OOO)"
            )

            # Wait for BOTH executions because they no longer share a task.
            await execution_outcome(runtime, sub1, timeout=900.0)
            await execution_outcome(runtime, sub2, timeout=900.0)
        finally:
            # A failed wait must still tear the runtime down; a leaked runtime's
            # retry storm poisons every test that runs after it in the file.
            await runtime.shutdown()

        counts = _thread_counts_by_prefix(tracker)
        assert counts.get("plate_1") == 2
        assert counts.get("final_plate") == 2, (
            f"STANDALONE keys should produce TWO disjoint final_plate receivers; "
            f"got {counts}"
        )

        final_tids = [
            tid for tid, name in tracker.thread_names.items()
            if name.startswith("final_plate")
        ]
        for tid in final_tids:
            methods = tracker.all_completed_snapshots.get(tid, [])
            combine_count = sum(1 for m in methods if m == "transfer_to_read_plate")
            assert combine_count == 1, (
                f"Each STANDALONE receiver gets exactly 1 contribution; "
                f"thread {tid}: {methods}"
            )
            for terminal in ("centrifuge", "read"):
                assert terminal in methods, (
                    f"Receiver {tid} should have completed {terminal}: {methods}"
                )
