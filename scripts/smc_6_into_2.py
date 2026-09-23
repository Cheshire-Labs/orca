"""Run the SMC assay with 6 sample plates routed into 2 final plates.

Two STANDALONE submissions, each with 3 plate_1 groups. STANDALONE gives
each submission its own execution and its own final_plate slot, so each
produces one final_plate receiver that collects its 3 contributors.

Run, from the repo root::

    python -m scripts.smc_6_into_2
"""
import asyncio
import logging
import sys
from uuid import uuid4

from examples.smc_assay.smc_assay_example import build_smc
from orca.plugins import MethodTracker
from orca.runtime.execution import ExecutionPhase
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.submission import BatchMode
from orca.runtime.system_runtime import SystemRuntime


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("smc_6_into_2")


def _plate_group(gid: str) -> LabwareGroup:
    return LabwareGroup(
        id=gid,
        members=(LabwareGroupMember(thread_template_name="plate_1_journey"),),
    )


async def main() -> None:
    smc = await build_smc()
    if smc.workflow is None:
        raise RuntimeError("build_smc returned no workflow")
    runtime = SystemRuntime(smc.system, event_bus=smc.event_bus)
    tracker = MethodTracker()
    runtime.register_plugin(tracker)
    await runtime.start()

    try:
        logger.info("Submitting sub1: 3 plate_1 groups (STANDALONE)")
        sub1 = await runtime.submit(
            smc.workflow,
            groups=[_plate_group(f"s1-g{i}") for i in range(3)],
            batch_mode=BatchMode.STANDALONE,
            mode=WorkflowRunMode.PURE_SIM,
        )

        logger.info("Submitting sub2: 3 plate_1 groups (STANDALONE)")
        sub2 = await runtime.submit(
            smc.workflow,
            groups=[_plate_group(f"s2-g{i}") for i in range(3)],
            batch_mode=BatchMode.STANDALONE,
            mode=WorkflowRunMode.PURE_SIM,
        )
        if sub1.execution_id == sub2.execution_id:
            raise RuntimeError("two STANDALONE submissions shared one execution")

        logger.info("Waiting for both executions to complete")
        for submission in (sub1, sub2):
            status = await asyncio.wait_for(
                runtime.wait_for_execution(submission), timeout=1800.0,
            )
            logger.info("Execution %s: %s", submission.execution_id[:8], status.status)
            if status.status is not ExecutionPhase.COMPLETED:
                raise RuntimeError(f"Execution ended {status.status}: {status.error}")
    finally:
        await runtime.shutdown()

    counts: dict[str, int] = {}
    for name in tracker.thread_names.values():
        prefix = name.rsplit("-", 1)[0]
        counts[prefix] = counts.get(prefix, 0) + 1
    logger.info("Thread counts: %s", counts)

    plate_1_count = counts.get("plate_1", 0)
    final_plate_count = counts.get("final_plate", 0)

    if plate_1_count != 6:
        logger.error("Expected 6 plate_1 threads, got %d", plate_1_count)
    else:
        logger.info("OK: 6 plate_1 threads")

    if final_plate_count != 2:
        logger.error("Expected 2 final_plate threads, got %d", final_plate_count)
    else:
        logger.info("OK: 2 final_plate threads")

    for tid, name in tracker.thread_names.items():
        if not name.startswith("final_plate"):
            continue
        methods = tracker.all_completed_snapshots.get(tid, [])
        combine_count = sum(1 for m in methods if m == "combine_plates")
        logger.info(
            "final_plate %s: %d combine_plates, terminal methods=%s",
            tid[:8], combine_count, [m for m in methods if m in ("transfer_eluate", "centrifuge", "read")],
        )

    assert plate_1_count == 6
    assert final_plate_count == 2


if __name__ == "__main__":
    asyncio.run(main())
