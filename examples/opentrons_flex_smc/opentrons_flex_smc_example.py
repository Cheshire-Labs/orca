"""Opentrons Flex SMC assay example: thin entry point for standalone runs + tests.

Models a representative bead-capture IL-6 immunoassay on an Opentrons Flex pair,
authoring every pipetting step as real inline liquid-handler methods
(``pick_up_tips`` / ``aspirate`` / ``dispense`` / ``discard_tips``) pushed via
PyLabRobot, instead of handing the device a protocol-file string. The wash /
incubate / spin / read steps run on dedicated devices.

Tests import ``build_opentrons_flex_smc`` for a fresh system per test run. The
same ``examples/opentrons_flex_smc`` folder is what a deployment harness
copies into a hosted deployment's ``deployment_package`` slot, so this is the single source of
the assay.
"""

import asyncio
import logging
import time

from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.sdk.build import SystemBuild


WORKFLOW_NAME = "opentrons_flex_smc_assay"

orca_logger = logging.getLogger("orca")


async def build_opentrons_flex_smc() -> SystemBuild:
    """Build a complete Opentrons Flex SMC assay system with fresh topology + workflow.

    Safe to call multiple times: every call creates fresh device instances for
    test isolation. Delegates to ``system.build`` so the standalone path and the
    deployment_package path construct the identical system.
    """
    from .system import build as build_system

    stores = InMemoryRuntimeStoreFactory()
    build = await build_system(stores)
    # Populate the single-workflow accessor. The deployment path (system.build)
    # leaves this None and drives by name; the standalone/test path has exactly
    # one workflow, so expose it for build.workflow consumers.
    build.workflow = build.system.get_workflow_template(WORKFLOW_NAME)
    return build


async def run(sim: bool) -> None:
    build = await build_opentrons_flex_smc()
    orca_logger.info("Starting Opentrons Flex SMC assay workflow execution.")
    await build.run(WorkflowRunMode.PURE_SIM if sim else WorkflowRunMode.LIVE)
    orca_logger.info("Opentrons Flex SMC assay workflow completed.")


if __name__ == "__main__":
    asyncio.run(run(True))
    orca_logger.info("Run completed successfully.")
    time.sleep(2)
