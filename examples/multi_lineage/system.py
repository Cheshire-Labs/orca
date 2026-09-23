"""Deployment-package shape for the multi-lineage example.

Wraps ``build_topology`` + ``build_workflow`` so a deployment harness
(or any other deployment-package consumer) can treat this folder as a
pluggable deployment_package: copy the folder into a hosted deployment's
``deployment_package`` slot and the runtime imports
``deployment_package.system:build(stores)`` to construct the system and
register the workflow.
"""

import orca.orca as orca
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.sdk.build import SystemBuild

from .topology import build_topology
from .workflow import build_workflow


async def build(stores: IRuntimeStoreFactory) -> SystemBuild:
    """Construct the multi-lineage system + register its workflow.

    Builds a fresh topology and workflow on every call so successive runs
    are isolated.
    """
    topology = build_topology(stores)
    workflow = build_workflow(topology)
    return await orca.build_system(
        name="Multi-Lineage",
        workflow=workflow,
        topology=topology,
        stores=stores,
    )
