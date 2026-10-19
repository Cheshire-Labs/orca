"""Venus Protocol Driver example: thin entry point for standalone runs + tests.

Demonstrates a workflow that runs Hamilton Venus methods and transfers
labware using a human transporter.

See ``topology.py`` and ``workflows/example_workflow.py`` for the physical
layout and workflow definition.

Run, from the repo root::

    python -m examples.simple_venus_example.simple_venus_example

This runs in simulation. To run on a Hamilton, mount the topology on ``orca start``
and run orca-client on the Hamilton PC with a ``venus`` device for ``ml_star`` and
a ``human`` device for ``human_transfer``. orca-client then prompts at each plate move.
"""

import asyncio
import logging

import orca.orca as orca

from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.sdk.build import SystemBuild


orca_logger = logging.getLogger("orca")

WORKFLOW_NAME = "example_workflow"


async def build_venus() -> SystemBuild:
    """Build the Venus example system. Every call makes fresh devices."""
    build = await orca.build_system_from_package(__package__, InMemoryRuntimeStoreFactory())
    # The package registers workflows by name; this runner runs the one it ships.
    build.workflow = build.system.get_workflow_template(WORKFLOW_NAME)
    return build


async def run(sim: bool) -> None:
    build = await build_venus()
    orca_logger.info("Starting Venus workflow execution.")
    await build.run(WorkflowRunMode.PURE_SIM if sim else WorkflowRunMode.LIVE)
    orca_logger.info("Venus workflow completed.")


if __name__ == "__main__":
    asyncio.run(run(sim=True))
