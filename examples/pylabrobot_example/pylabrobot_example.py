"""Learn Orca in One File -- Comprehensive PLR Example.

Thin entry point composing topology + workflow. See ``topology.py`` and
``workflow.py`` for the physical layout and workflow definition.

Demonstrates every Orca SDK feature:
 1. Labware templates (PlateTemplate, TipRackTemplate, TroughTemplate)
 2. Devices with PLR + sim drivers, ResourcePool
 3. Teachpoints with CartesianCoordinates + Transporter
 4. Actions: PLR cherry pick (CSV worklist, two plates), serial dilution (trough)
 5. AnyLabwareTemplate, failure_policy, tag, ctx.param, ctx.emit, ctx.wait_for
 6. Methods (bare + with failure_policy)
 7. Threads with orca.join (contributor labware) and orca.branch (QC pass/fail)
 8. SystemBoundEventHandler for observability
 9. Workflow: wf.start, wf.thread, wf.on, wf.variable
10. build_system, then SystemBuild.run, which runs the workflow once on a SystemRuntime

Workflow story:
  1. Cherry pick from sample_plate to dest_plate (worklist CSV)
  2. Serial dilute dest_plate column B down to column C (diluent from trough)
  3. Shake dest_plate (shaker pool)
  4. Read dest_plate (plate reader, emits absorbance data)
  5. Evaluate QC (pass/fail based on avg absorbance)
  6. Branch: pass -> seal, fail -> re-dilute + re-read

Run, from the repo root:
  python -m examples.pylabrobot_example.pylabrobot_example
"""

import asyncio
import logging
import sys

import orca.orca as orca

from examples.pylabrobot_example.topology import build_topology
from examples.pylabrobot_example.workflow import build_workflow
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.sdk.build import SystemBuild

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
orca_logger = logging.getLogger("orca")


async def build_plr() -> SystemBuild:
    """Build a complete PLR example system with fresh topology and workflow.

    Safe to call multiple times: every call creates fresh device instances
    for test isolation.

    Tests that need to inject a custom LH driver (e.g. ``RecordingLiquidHandlerDriver``)
    bind a one-shot factory via ``use_device_factory(...)`` before calling
    ``build_plr()``; ``build_topology`` honors the outer factory rather than
    falling back to its default Chatterbox-on-LH binding.
    """
    stores = InMemoryRuntimeStoreFactory()
    topology = build_topology(stores)
    workflow = build_workflow(topology)
    return await orca.build_system(
        name="Learn Orca",
        workflow=workflow,
        topology=topology,
        stores=stores,
        description="Comprehensive example demonstrating every Orca SDK feature",
    )


async def run(sim: bool = True) -> None:
    build = await build_plr()
    orca_logger.info("Starting Learn Orca workflow execution.")
    await build.run(WorkflowRunMode.PURE_SIM if sim else WorkflowRunMode.LIVE)
    orca_logger.info("Learn Orca workflow completed.")


if __name__ == "__main__":
    asyncio.run(run(True))
