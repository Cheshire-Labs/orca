"""End-to-end tests for the PURE_SIM SMC demo.

The demo's own `run()` is invoked exactly as the operator would
(`python -m examples.smc_assay.run_pure_sim`), so a broken demo body fails
here, not on a customer's machine. The non-demo SDK SMC test in
`test_sdk_smc_assay.py` covers richer assertions.

Marked slow: each SMC assay run is ~50 actions in sim, ~30-60s on a
developer machine.
"""

import pytest

from tests.test_helpers import execution_outcome

from examples.smc_assay.smc_assay_example import build_smc
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.state.records import DeviceOperation


@pytest.mark.slow
@pytest.mark.timeout(180)
@pytest.mark.asyncio
async def test_smc_pure_sim_demo_runs_end_to_end() -> None:
    """Run the demo's sequence on a SystemRuntime directly: sim still writes ops records."""
    smc = await build_smc()
    assert smc.workflow is not None
    runtime = SystemRuntime(smc.system, event_bus=smc.event_bus)
    await runtime.start()
    try:
        submission = await runtime.submit(smc.workflow, mode=WorkflowRunMode.PURE_SIM)
        final_status = await execution_outcome(runtime, submission, timeout=150.0)
        assert final_status.status == "completed", (
            f"Expected completed, got {final_status.status}: {final_status.error}"
        )
        # Ops land in the run's execution bucket (the system view holds only
        # out-of-execution writes), so read the bucket the run wrote.
        records = await smc.system.ops_history.for_execution(
            submission.execution_id).records()
        assert len(records) > 0, "PURE_SIM run produced no OpsHistory records"
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.timeout(400)
@pytest.mark.asyncio
async def test_the_demo_reports_the_records_its_actions_wrote() -> None:
    """`run()` completes and returns records of real device operations.

    The un-awaited `build_smc()` regression shipped because no test called the
    demo body. The demo once counted the system's labware seed records as the run's.
    """
    import examples.smc_assay.run_pure_sim as demo

    records = await demo.run()
    operations = {op.operation for record in records for op in record.operations}
    seeding = {DeviceOperation.INITIAL_STATE, DeviceOperation.OBSERVATION_GAP}
    assert operations - seeding, operations


def test_demo_module_is_importable() -> None:
    """The demo module imports cleanly with the same paths the script uses.

    Catches a class of breakage where the demo's imports drift away
    from what the package exports (e.g., a renamed `build_smc` would
    break the demo silently because nobody else imports
    `examples.smc_assay.run_pure_sim`).
    """
    import examples.smc_assay.run_pure_sim as demo

    assert callable(demo.run)
    assert callable(demo.main)
