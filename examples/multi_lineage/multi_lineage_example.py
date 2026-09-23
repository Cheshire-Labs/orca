"""Abstract multi-lineage example: thin entry point composing topology + workflow.

N parallel sample lineages feeding one shared reservoir. Non-SMC worked
example for the T6 primitives:
- ``LabwareGroup`` + ``LabwareGroupMember``: one group per sample plate; a
  submission can carry N groups and the engine spawns N sample threads from
  identical workflow code. N=1 and N=3 use the exact same workflow definition;
  the group count is a submission-time parameter.
- ``GroupSharing.SHARED_ACROSS_GROUPS`` + ``SubmissionBatching.BATCHABLE``
  on the reservoir: the slot key collapses group + submission dimensions so
  every sample across every group in the submission feeds one physical
  reservoir. A later submission can opt into the same receiver via
  ``BatchMode.JOIN_EXISTING``.
- ``@orca.thread(contributes_to=[...])`` on the sample: declares that sample
  threads feed the reservoir's slot; the engine closes the slot when every
  registered contributor thread has terminated.
- ``while ctx.has_more_work(): yield orca.join(...)``: the reservoir receiver
  pattern -- accept contributions one by one, exit when the close signal fires.

Run, from the repo root::

    python -m examples.multi_lineage.multi_lineage_example

    # Or with a different group count:
    python -m examples.multi_lineage.multi_lineage_example --groups 5
"""
import argparse
import asyncio
import logging
import sys
from uuid import uuid4

import orca.orca as orca

from examples.multi_lineage.topology import build_topology
from examples.multi_lineage.workflow import build_workflow
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory, IRuntimeStoreFactory
from orca.sdk.build import SystemBuild

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("multi_lineage")


async def build_multi_lineage(
    stores: IRuntimeStoreFactory | None = None,
) -> SystemBuild:
    """Build a complete multi-lineage system with fresh topology and workflow.

    Safe to call multiple times: every call creates fresh device instances
    for test isolation. The returned ``SystemBuild`` carries the store
    factory used to construct per-device stores at build time.
    """
    factory = stores if stores is not None else InMemoryRuntimeStoreFactory()
    topology = build_topology(factory)
    workflow = build_workflow(topology)
    return await orca.build_system(
        name="multi_lineage_system",
        workflow=workflow,
        topology=topology,
        stores=factory,
    )


def _sample_group() -> LabwareGroup:
    return LabwareGroup(
        id=str(uuid4()),
        members=(LabwareGroupMember(thread_template_name="sample"),),
    )


async def main(group_count: int) -> None:
    logger.info("Building multi-lineage system (group_count=%d)...", group_count)
    ml = await build_multi_lineage()
    groups = [_sample_group() for _ in range(group_count)]
    logger.info("Running with %d groups...", len(groups))
    await asyncio.wait_for(ml.run(WorkflowRunMode.PURE_SIM, groups=groups), timeout=600.0)
    logger.info("Execution completed")


if __name__ == "__main__":
    description = (__doc__ or "").split("\n", 1)[0]
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--groups", type=int, default=3,
        help="Number of sample lineages (groups) to submit. Defaults to 3.",
    )
    args = parser.parse_args()
    asyncio.run(main(args.groups))
