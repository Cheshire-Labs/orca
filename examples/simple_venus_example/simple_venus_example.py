"""Venus Protocol Driver example: thin entry point composing topology + workflow.

Demonstrates a workflow that runs Hamilton Venus methods and transfers
labware using a human transporter.

See ``topology.py`` and ``workflow.py`` for the physical layout and workflow
definition.

Run, from the repo root::

    python -m examples.simple_venus_example.simple_venus_example

This runs in simulation. To run on a Hamilton, mount the topology on ``orca start``
and run orca-client on the Hamilton PC with a ``venus`` device for ``ml_star`` and
a ``human`` device for ``human_transfer``. orca-client then prompts at each plate move.
"""

import asyncio
import logging

import orca.orca as orca

from examples.simple_venus_example.topology import build_topology
from examples.simple_venus_example.workflow import build_workflow
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.sdk.build import SystemBuild


orca_logger = logging.getLogger("orca")


async def build_venus() -> SystemBuild:
    """Build a complete Venus example with fresh topology and workflow.

    Safe to call multiple times: every call creates fresh device instances
    for test isolation.
    """
    stores = InMemoryRuntimeStoreFactory()
    topology = build_topology(stores)
    workflow = build_workflow(topology)
    return await orca.build_system(
        "Venus Example",
        workflow=workflow,
        topology=topology,
        stores=stores,
        description="Venus Example System",
    )


async def run(sim: bool) -> None:
    build = await build_venus()
    orca_logger.info("Starting Venus workflow execution.")
    await build.run(WorkflowRunMode.PURE_SIM if sim else WorkflowRunMode.LIVE)
    orca_logger.info("Venus workflow completed.")


if __name__ == "__main__":
    asyncio.run(run(sim=True))
