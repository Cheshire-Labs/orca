"""Hamilton SMC assay example: thin entry point for standalone runs + tests.

Models a representative bead-capture IL-6 immunoassay on a Hamilton ML STAR pair,
authoring every pipetting step as real inline liquid-handler methods
(``pick_up_tips`` / ``aspirate`` / ``dispense`` / ``discard_tips``) pushed via
PyLabRobot, instead of handing the device a protocol-file string. The wash /
incubate / spin / read steps run on dedicated devices.

Tests import ``build_hamilton_smc`` for a fresh system per test run.
"""

import asyncio
import logging
import time

import orca.orca as orca

from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.sdk.build import SystemBuild


WORKFLOW_NAME = "hamilton_smc_assay"

orca_logger = logging.getLogger("orca")


async def build_hamilton_smc() -> SystemBuild:
    """Build the Hamilton SMC assay system. Every call makes fresh devices."""
    build = await orca.build_system_from_package(__package__, InMemoryRuntimeStoreFactory())
    # The package registers workflows by name; this runner runs the one it ships.
    build.workflow = build.system.get_workflow_template(WORKFLOW_NAME)
    return build


async def run(sim: bool) -> None:
    build = await build_hamilton_smc()
    orca_logger.info("Starting Hamilton SMC assay workflow execution.")
    await build.run(WorkflowRunMode.PURE_SIM if sim else WorkflowRunMode.LIVE)
    orca_logger.info("Hamilton SMC assay workflow completed.")


if __name__ == "__main__":
    asyncio.run(run(True))
    orca_logger.info("Run completed successfully.")
    time.sleep(2)
