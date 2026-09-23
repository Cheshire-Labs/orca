"""Deployment-package shape for the SMC assay example.

Wraps ``build_topology`` + ``build_workflow`` so a deployment
harness (and any other deployment-package consumer) can treat this
folder as a pluggable deployment_package: copy the folder into
a hosted deployment's ``deployment_package`` slot and the runtime imports
``deployment_package.system:build(stores)`` to construct the system
and register the workflow.

Relative imports keep this portable: when the folder is renamed
``deployment_package`` at deployment time, ``from .topology import
build_topology`` continues to resolve.

There is no ``default_run_mode`` on ``build_system``. Every submission
declares its own ``run_mode`` (PURE_SIM / DEVICE_SIM / LIVE) at submit
time; the deployment_package does not pre-declare a default. The
lab-sim harness picks the mode at submission time via
``mode=WorkflowRunMode[os.environ["LAB_SIM_DEFAULT_RUN_MODE"]]`` or
similar.
"""

import orca.orca as orca
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.sdk.build import SystemBuild

from .topology import build_topology
from .workflow import build_workflow


async def build(stores: IRuntimeStoreFactory) -> SystemBuild:
    """Construct the SMC assay system + register its workflow.

    Mirrors ``examples/smc_assay/smc_assay_example.py:build_smc`` but
    adapts the signature to the deployment-package shape ``build(stores)
    -> SystemBuild``. Builds a fresh topology and workflow on every call
    so successive runs are isolated.
    """
    topology = build_topology(stores)
    workflow = build_workflow(topology)
    return await orca.build_system(
        name="SMC Assay",
        workflow=workflow,
        topology=topology,
        stores=stores,
    )
