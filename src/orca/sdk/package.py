"""Build a system from a package that holds `topology.py` and `workflows/*.py`.

`topology.py` defines `build_topology()` and may define `SYSTEM_NAME` and
`SYSTEM_DESCRIPTION`. Each `workflows/<name>.py` defines
`build_workflow(topology)` and may define a `reservoirs` list of deck-resident
labware templates. The package needs no `__init__.py`.
"""

import importlib
import os
import pkgutil
from pathlib import Path
from collections.abc import Sequence
from types import ModuleType

from orca.resource_models.labware import LabwareTemplate
from orca.resource_models.labware_placement import IUnsavedPositions
from orca.resource_models.labware_service_interface import ILabwareService
from orca.driver_management.device_factory_context import use_device_factory
from orca.driver_management.device_factory_protocol import IDeviceDriverProvider
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.state.ops_store import IOpsHistoryStore
from orca.events.event_bus_interface import IEventBus
from orca.runtime.startup import in_memory_labware_service, in_memory_unsaved_positions
from orca.sdk.build import SystemBuild, Topology, add_workflow_template, build_system_over
from orca.workflow_models.workflow_templates import WorkflowTemplate


__all__ = ["build_system", "build_topology_from_module", "build_system_from_package"]


async def build_system(
    name: str,
    topology: Topology,
    stores: IRuntimeStoreFactory,
    workflow: WorkflowTemplate | None = None,
    labwares: Sequence[LabwareTemplate] | None = None,
    description: str = "",
    *,
    event_bus: IEventBus,
    ops_history_store: IOpsHistoryStore,
) -> SystemBuild:
    """Build a system from a physical topology, and optionally one workflow, as a local run does.

    Its labware record is held in memory. A server builds through `build_system_from_package` over its own.
    """
    return await build_system_over(
        name, topology, stores, workflow, labwares, description,
        event_bus=event_bus, ops_history_store=ops_history_store,
        labware_service=in_memory_labware_service(), unsaved_positions=in_memory_unsaved_positions(),
    )


def build_topology_from_module(module: ModuleType) -> Topology:
    """Call the module's `build_topology()` and check it returned a Topology."""
    build_topology = getattr(module, "build_topology", None)
    if not callable(build_topology):
        raise AttributeError(f"{module.__name__} has no build_topology() function")
    topology = build_topology()
    if not isinstance(topology, Topology):
        raise TypeError(
            f"{module.__name__}.build_topology returned {type(topology).__name__}; "
            "expected orca.sdk.build.Topology",
        )
    return topology


def _import_package(package_name: str) -> None:
    importlib.invalidate_caches()
    package = importlib.import_module(package_name)
    # One folder can be listed under several sys.path spellings ("" and an
    # absolute path under `python -m`), so count distinct real folders.
    folders = sorted({
        os.path.normcase(str(Path(entry).resolve()))
        for entry in getattr(package, "__path__", [])
    })
    if len(folders) > 1:
        raise ImportError(
            f"{package_name!r} resolves to more than one folder on sys.path: {folders}. "
            "Keep one folder of that name importable.",
        )


def _import_workflow_modules(package_name: str) -> list[ModuleType]:
    workflows_name = f"{package_name}.workflows"
    try:
        workflows = importlib.import_module(workflows_name)
    except ModuleNotFoundError as exc:
        if exc.name != workflows_name:
            raise
        return []
    modules: list[ModuleType] = []
    for info in pkgutil.iter_modules(workflows.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{workflows_name}.{info.name}")
        if callable(getattr(module, "build_workflow", None)):
            modules.append(module)
    return modules


def _build_workflow(module: ModuleType, topology: Topology) -> WorkflowTemplate:
    workflow = module.build_workflow(topology)
    if not isinstance(workflow, WorkflowTemplate):
        raise TypeError(
            f"{module.__name__}.build_workflow returned {type(workflow).__name__}; "
            "expected WorkflowTemplate",
        )
    return workflow


def _system_name(topology_module: ModuleType, package_name: str) -> str:
    name = getattr(topology_module, "SYSTEM_NAME", None)
    if name is None:
        return package_name.rsplit(".", 1)[-1]
    if not isinstance(name, str):
        raise TypeError(f"{topology_module.__name__}.SYSTEM_NAME must be a string")
    return name


def _system_description(topology_module: ModuleType) -> str:
    description = getattr(topology_module, "SYSTEM_DESCRIPTION", None)
    if description is None:
        return ""
    if not isinstance(description, str):
        raise TypeError(f"{topology_module.__name__}.SYSTEM_DESCRIPTION must be a string")
    return description


async def build_system_from_package(
    package_name: str,
    stores: IRuntimeStoreFactory,
    ops_history_store: IOpsHistoryStore,
    device_factory: IDeviceDriverProvider,
    labware_service: ILabwareService,
    unsaved_positions: IUnsavedPositions,
    *,
    event_bus: IEventBus,
) -> SystemBuild:
    """Build the system a package's `topology.py` and `workflows/*.py` declare.

    `device_factory` supplies every device's driver while the package imports and `topology.py` builds.
    """
    # Settings first: they are separate from the topology and may one day shape how it loads.
    config = stores.system_config()
    # Imports are inside the binding too: a device a module builds at import must get the server's driver.
    with use_device_factory(device_factory):
        _import_package(package_name)
        topology_module = importlib.import_module(f"{package_name}.topology")
        topology = build_topology_from_module(topology_module)
        workflow_modules = _import_workflow_modules(package_name)
    reservoirs: list[LabwareTemplate] = []
    for module in workflow_modules:
        reservoirs.extend(getattr(module, "reservoirs", []))

    build = await build_system_over(
        name=_system_name(topology_module, package_name),
        topology=topology,
        stores=stores,
        labwares=reservoirs,
        description=_system_description(topology_module),
        event_bus=event_bus,
        ops_history_store=ops_history_store,
        labware_service=labware_service,
        unsaved_positions=unsaved_positions,
        config=config,
    )
    for module in workflow_modules:
        await add_workflow_template(build.system, _build_workflow(module, topology))
    return build
