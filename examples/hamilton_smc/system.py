"""Deployment-package shape for the Hamilton SMC assay.

``build(stores)`` constructs the topology, auto-discovers every workflow module
under ``workflows/`` that exposes ``build_workflow(topology)``, and registers
each on the resulting ``SystemBuild``. Copy this folder into a hosted deployment's
``deployment_package`` slot and the runtime imports
``deployment_package.system:build(stores)``.

Imports are package-relative, so the same folder resolves whether it is imported
as ``examples.hamilton_smc`` (orca-core tests / direct runs) or renamed
``deployment_package`` at deployment time. Discovery walks ``{__package__}.workflows``
rather than a hardcoded module path for the same reason.

The workflow lives at ``workflows/hamilton_smc_assay.py`` like any other
workflow, so it is a normal discoverable, deletable workflow file rather than
glue hardcoded into this module. Stage the folder without that file to boot the
same bench with no workflow registered.

Deck-resident reagents are pre-declared via ``labwares=``: each workflow module
that exposes a ``reservoirs`` list contributes its residents so the runtime seeds
them onto the ML STAR decks and the ``REUSE_EXISTING`` resident threads bind to
the persistent instances.

Run mode is set per-submission at submit time. To dispatch over the WebSocket to
orca-client's PLR-backed drivers, submit each execution with ``run_mode="LIVE"``.
"""

import importlib
import pkgutil

import orca.orca as orca
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.sdk.build import SystemBuild, add_workflow_template
from orca.sdk.labware import LabwareTemplate

from .topology import build_topology


async def build(stores: IRuntimeStoreFactory) -> SystemBuild:
    """Construct the Hamilton SMC system + auto-register its workflows."""
    topology = build_topology(stores)

    workflows_pkg = f"{__package__}.workflows"
    pkg_module = importlib.import_module(workflows_pkg)
    builders = []
    reservoirs: list[LabwareTemplate] = []
    for module_info in pkgutil.iter_modules(pkg_module.__path__):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{workflows_pkg}.{module_info.name}")
        builder = getattr(module, "build_workflow", None)
        if builder is None:
            continue
        builders.append(builder)
        reservoirs.extend(getattr(module, "reservoirs", []))

    system_build = await orca.build_system(
        name="Hamilton SMC Assay",
        topology=topology,
        stores=stores,
        labwares=reservoirs,
        description="SMC immunoassay authoring real inline liquid-handler methods "
                    "(aspirate/dispense/tips) pushed via PyLabRobot on a Hamilton "
                    "ML STAR pair (mlstar_1 capture/detection, mlstar_2 elution/neutralize)",
    )

    for builder in builders:
        await add_workflow_template(system_build.system, builder(topology))

    return system_build
