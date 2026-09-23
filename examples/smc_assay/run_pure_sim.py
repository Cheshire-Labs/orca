"""Run the SMC assay once in PURE_SIM and report the records its actions wrote.

Run, from the repo root::

    python -m examples.smc_assay.run_pure_sim

PURE_SIM runs every device on in-process simulators, so no orca-client is needed.
"""

import asyncio
import logging

from examples.smc_assay.smc_assay_example import build_smc
from orca.runtime.run_modes import WorkflowRunMode
from orca.state.records import TrackingRecord


orca_logger = logging.getLogger("orca")


async def run() -> list[TrackingRecord]:
    """Run the assay once and return the records written under its execution."""
    smc = await build_smc()
    orca_logger.info("Starting SMC assay in PURE_SIM mode")
    status = await asyncio.wait_for(smc.run(WorkflowRunMode.PURE_SIM), timeout=300.0)
    records = await smc.system.ops_history.for_execution(status.id).records()
    orca_logger.info("OpsHistory captured %d records during the run", len(records))
    return records


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()
