"""SMC Assay example: thin entry point composing topology + workflow.

The design pattern for orca examples:
  * ``topology.py`` -- ``build_topology()`` returns the physical layout.
  * ``workflow.py`` -- ``build_workflow(topology)`` returns the workflow
    template, closing over devices pulled from the topology.
  * This file -- glues them together with ``build_system()`` and provides
    ``build_smc()`` / ``run()`` for scripts and tests.

Tests import ``build_smc`` for a fresh system per test run.
"""

import asyncio
import logging
import time

import orca.orca as orca

from examples.smc_assay.topology import build_topology
from examples.smc_assay.workflow import build_workflow
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.sdk.build import SystemBuild


orca_logger = logging.getLogger("orca")

SmcAssayBuild = SystemBuild


async def build_smc() -> SystemBuild:
    """Build a complete SMC assay with fresh topology, workflow, and system.

    Safe to call multiple times: every call creates fresh device instances
    for test isolation.
    """
    stores = InMemoryRuntimeStoreFactory()
    topology = build_topology(stores)
    workflow = build_workflow(topology)
    return await orca.build_system(
        name="SMC Assay",
        workflow=workflow,
        topology=topology,
        stores=stores,
    )


async def run(sim: bool) -> None:
    build = await build_smc()
    orca_logger.info("Starting SMC Assay workflow execution.")
    await build.run(WorkflowRunMode.PURE_SIM if sim else WorkflowRunMode.LIVE)
    orca_logger.info("SMC Assay workflow completed.")


if __name__ == "__main__":
    asyncio.run(run(True))
    orca_logger.info("Run completed successfully.")
    time.sleep(2)
