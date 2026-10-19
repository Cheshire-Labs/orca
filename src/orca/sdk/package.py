"""Build a system from a package that holds `topology.py` and `workflows/*.py`.

`topology.py` defines `build_topology(stores)` and may define `SYSTEM_NAME`,
`SYSTEM_DESCRIPTION` and `SIM_DRIVERS`. Each `workflows/<name>.py` defines
`build_workflow(topology)` and may define a `reservoirs` list of deck-resident
labware templates. The package needs no `__init__.py`.
"""

import importlib
import os
import pkgutil
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

from orca.resource_models.labware import LabwareTemplate
from orca.runtime.device_factory import SimDeviceFactory
from orca.runtime.device_factory_context import is_factory_bound, use_device_factory
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.sdk.build import SystemBuild, Topology, add_workflow_template, build_system
from orca.workflow_models.workflow_templates import WorkflowTemplate


SimDriverFactories = Mapping[str, Callable[[], DriverPairElement]]


class _DeclaredSimDriverFactory:
    """A listed device type gets one declared driver in both slots; the rest get SimDeviceFactory's pair."""

    def __init__(self, sim_drivers: SimDriverFactories) -> None:
        self._sim_drivers = dict(sim_drivers)
        self._fallback = SimDeviceFactory()

    def build_drivers(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        make_driver = self._sim_drivers.get(device_type)
        if make_driver is None:
            return self._fallback.build_drivers(device_type, name, deck_modeling=deck_modeling)
        driver = make_driver()
        return driver, driver


def _declared_sim_drivers(module: ModuleType) -> SimDriverFactories | None:
    declared = getattr(module, "SIM_DRIVERS", None)
    if declared is None:
        return None
    if not isinstance(declared, Mapping):
        raise TypeError(
            f"{module.__name__}.SIM_DRIVERS must map a device type to a zero-argument "
            f"driver factory, got {type(declared).__name__}",
        )
    return declared


@contextmanager
def declared_sim_drivers_bound(module: ModuleType) -> Iterator[None]:
    """Bind the module's SIM_DRIVERS for the block, unless a device factory is already bound."""
    if is_factory_bound():
        yield
        return
    sim_drivers = _declared_sim_drivers(module)
    if sim_drivers is None:
        yield
        return
    with use_device_factory(_DeclaredSimDriverFactory(sim_drivers)):
        yield


def build_topology_from_module(module: ModuleType, stores: IRuntimeStoreFactory) -> Topology:
    """Call the module's `build_topology(stores)` with its SIM_DRIVERS bound when nothing else is."""
    build_topology = getattr(module, "build_topology", None)
    if not callable(build_topology):
        raise AttributeError(f"{module.__name__} has no build_topology(stores) function")
    with declared_sim_drivers_bound(module):
        topology = build_topology(stores)
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
    package_name: str | None, stores: IRuntimeStoreFactory,
) -> SystemBuild:
    """Build the system a package's `topology.py` and `workflows/*.py` declare."""
    if not package_name:
        raise ValueError(
            "build_system_from_package needs a package name. A module run as a script "
            "has none; run it with python -m so __package__ is set.",
        )
    _import_package(package_name)
    topology_module = importlib.import_module(f"{package_name}.topology")
    topology = build_topology_from_module(topology_module, stores)

    workflow_modules = _import_workflow_modules(package_name)
    reservoirs: list[LabwareTemplate] = []
    for module in workflow_modules:
        reservoirs.extend(getattr(module, "reservoirs", []))

    build = await build_system(
        name=_system_name(topology_module, package_name),
        topology=topology,
        stores=stores,
        labwares=reservoirs,
        description=_system_description(topology_module),
    )
    for module in workflow_modules:
        await add_workflow_template(build.system, _build_workflow(module, topology))
    return build
