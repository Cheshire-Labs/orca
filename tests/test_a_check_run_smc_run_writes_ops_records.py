"""A CHECK_RUN run of the SMC assay writes operation-history records.

`test_sdk_smc_assay.py` covers the run's method order and labware journeys.

Marked slow: each SMC assay run is ~50 actions in sim, ~30-60s on a
developer machine.
"""

import pytest

from tests.test_helpers import execution_outcome, state_every_liquid_handler_head_empty

from tests.example_builds import build_smc
from orca.modes.run_modes import WorkflowRunMode
from tests.memory_activity import memory_runtime


@pytest.mark.slow
@pytest.mark.timeout(180)
@pytest.mark.asyncio
async def test_smc_check_run_demo_runs_end_to_end() -> None:
    """Sim still writes ops records."""
    smc = await build_smc()
    assert smc.workflow is not None
    runtime = await memory_runtime(smc.system, event_bus=smc.event_bus)
    await runtime.start()
    await state_every_liquid_handler_head_empty(runtime)
    try:
        submission = await runtime.submit(smc.workflow, mode=WorkflowRunMode.CHECK_RUN)
        final_status = await execution_outcome(runtime, submission, timeout=150.0)
        assert final_status.status == "completed", (
            f"Expected completed, got {final_status.status}: {final_status.error}"
        )
        # Ops land in the run's execution bucket (the system view holds only
        # out-of-execution writes), so read the bucket the run wrote.
        records = await smc.system.ops_history.for_execution(
            submission.execution_id).records()
        assert len(records) > 0, "CHECK_RUN run produced no OpsHistory records"
    finally:
        await runtime.shutdown()

