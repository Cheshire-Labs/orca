"""SMC Assay example: thin entry point for standalone runs + tests.

The design pattern for orca examples:
  * ``topology.py`` -- ``build_topology()`` returns the physical layout.
  * ``workflows/smc_assay.py`` -- ``build_workflow(topology)`` returns the
    workflow template, closing over devices pulled from the topology.
  * This file -- builds the system from the package and provides
    ``build_smc()`` / ``run()`` for scripts and tests.

Tests import ``build_smc`` for a fresh system per test run.
"""

import asyncio
import logging
import time

import orca.orca as orca

from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.sdk.build import SystemBuild


orca_logger = logging.getLogger("orca")

SmcAssayBuild = SystemBuild

WORKFLOW_NAME = "smc_assay"


async def build_smc() -> SystemBuild:
    """Build the SMC assay system. Every call makes fresh devices."""
    build = await orca.build_system_from_package(__package__, InMemoryRuntimeStoreFactory())
    # The package registers workflows by name; this runner runs the one it ships.
    build.workflow = build.system.get_workflow_template(WORKFLOW_NAME)
    return build


async def run(sim: bool) -> None:
    build = await build_smc()
    orca_logger.info("Starting SMC Assay workflow execution.")
    await build.run(WorkflowRunMode.PURE_SIM if sim else WorkflowRunMode.LIVE)
    orca_logger.info("SMC Assay workflow completed.")


if __name__ == "__main__":
    asyncio.run(run(True))
    orca_logger.info("Run completed successfully.")
    time.sleep(2)
