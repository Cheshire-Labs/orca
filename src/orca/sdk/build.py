"""High-level system builder for the Python SDK."""

from dataclasses import dataclass, field
from typing import Iterable, List, Mapping, Sequence, TypeVar

from orca.config import OrcaConfig
from orca.resource_models.labware_placement import IUnsavedPositions
from orca.resource_models.labware_service_interface import ILabwareService
from orca.events.event_bus_interface import IEventBus
from orca.devices.device_interfaces import ITrackedDevice
from orca.devices.devices import LiquidHandler
from orca.devices.deck_gripper_transporter import DeckGripperTransporter
from orca.resource_models.deck_site import DeckSite
from orca.resource_models.deck_site_location import DeckSiteLocation
from orca.resource_models.device_deck_site import DeviceDeckSite
from orca.resource_models.labware_staging_bridge import LabwareStagingBridge
from orca.resource_models.devices import Device
from orca.resource_models.labware import LabwareTemplate
from orca.resource_models.labware_placeable_interface import ILabwarePlaceable
from orca.resource_models.location import Location
from orca.resource_models.site_rack import SiteRack
from orca.runtime.device_assignment import assign_devices
from cheshire_drivers.gateway_protocol import AssignedDevice
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.resources import IResource
from orca.resource_models.tracking_interpreter import DefaultInterpreter
from orca.devices.interpreter_resolution import resolve_operation_interpreter
from orca.resource_models.transporter import Transporter
from orca.state.ops_store import IOpsHistoryStore
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.variables.variable_store import IVariableService
from orca.system.deck_sites import enumerate_deck_sites
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.resource_models.resource_registry import ResourceRegistry
from orca.system.system import System
from orca.system.system_interface import ISystem
from orca.system.system_map import SystemMap
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate


__all__ = [
    "Topology",
    "SystemBuild",
    "add_workflow_template",
    "compile_workflow_code",
]


class TrackedDeviceInterpreterError(RuntimeError):
    """Raised at build time when an ITrackedDevice resolves to DefaultInterpreter.

    Tracked devices declare ``operation_interpreter()`` via the ITrackedDevice
    contract. Falling through to DefaultInterpreter means actions on this
    device would silently emit only generic records, dropping the typed
    per-command details the interpreter was meant to produce. The build-time
    walk catches this before deployment instead of waiting for ops_history
    to be quietly empty in production.
    """



# Devices and passive placeables are both valid location values; Device is
# not an ILabwarePlaceable on its own -- build_system wraps it in a
# LabwareStagingBridge at runtime. We accept either shape here.
LocationValue = Device | ILabwarePlaceable | SiteRack

_DeviceT = TypeVar("_DeviceT", bound=LocationValue)


@dataclass
class Topology:
    """Physical layout of the lab: locations, transporters, and device pools.

    Separating topology from workflow lets one topology serve many workflows
    (and one workflow run on different topologies, e.g. sim vs hardware).

    Args:
        locations: Named physical positions that can hold labware. Values are
            devices (auto-wrapped in a gateway), passive pads, or plate hotels,
            whose shelves are each a pad at ``<hotel>/shelf_<n>``.
        transporters: Robotic arms and translators that move labware between
            locations.
        pools: Multi-device resource pools (e.g. a bank of 10 shakers treated
            as one addressable pool). Single-device pools are created
            automatically from actions that reference a device directly.
    """
    locations: Mapping[str, LocationValue]
    transporters: list[Transporter]
    pools: list[ResourcePool] = field(default_factory=list)

    def device(self, name: str, expected_type: type[_DeviceT]) -> _DeviceT:
        """Fetch a location's resource, narrowed to ``expected_type``.

        Lets workflow authors write ``t.device("bravo_96", LiquidHandler)``
        and get a properly-typed reference for ``@orca.action(device=...)``
        without losing type information through dict lookups.
        """
        if name not in self.locations:
            available = ", ".join(sorted(self.locations.keys()))
            raise KeyError(f"No location named {name!r}. Available: {available}")
        placeable = self.locations[name]
        if not isinstance(placeable, expected_type):
            raise TypeError(
                f"Location {name!r} is {type(placeable).__name__}, "
                f"expected {expected_type.__name__}"
            )
        return placeable

    def pool(self, name: str) -> ResourcePool:
        """Fetch a multi-device pool by name."""
        for candidate in self.pools:
            if candidate.name == name:
                return candidate
        available = ", ".join(p.name for p in self.pools)
        raise KeyError(f"No pool named {name!r}. Available: {available}")


@dataclass
class SystemBuild:
    """Top-level result of `build_system`.

    `workflow` is the workflow passed to `build_system`, or None. Servers pass
    none and register each workflow with `add_workflow_template`.

    `topology` is the Topology passed in, so a workflow registered after the
    build can call `build_workflow(topology)` against the same devices.

    `config` is the settings the system was built with.
    """
    system: System
    workflow: WorkflowTemplate | None
    event_bus: IEventBus
    topology: "Topology | None" = None
    device_assignment: list[AssignedDevice] = field(default_factory=list)
    config: OrcaConfig = field(default_factory=OrcaConfig)


def _attach_setup_files(topology: Topology, stores: IRuntimeStoreFactory) -> None:
    for transporter in topology.transporters:
        transporter.bind_teachpoints(stores.teachpoints(transporter.name))
    for resource in topology.locations.values():
        if isinstance(resource, LiquidHandler):
            resource.bind_deck_layouts(stores.deck_layouts(resource.name))


async def _with_global_variables(stores: IRuntimeStoreFactory) -> IVariableService:
    variable_store = stores.variable_store()
    entries = await stores.global_variables().read()
    variable_store.register_global_definitions({name: e for name, e in entries.items() if e.is_declared})
    for name, entry in entries.items():
        if entry.value is not None:
            variable_store.set_global(name, entry.value)
    return variable_store


async def build_system_over(
    name: str,
    topology: Topology,
    stores: IRuntimeStoreFactory,
    workflow: WorkflowTemplate | None = None,
    labwares: Sequence[LabwareTemplate] | None = None,
    description: str = "",
    *,
    event_bus: IEventBus,
    ops_history_store: IOpsHistoryStore,
    labware_service: ILabwareService,
    unsaved_positions: IUnsavedPositions,
    config: OrcaConfig | None = None,
) -> SystemBuild:
    """Build a system from a physical topology, and optionally one workflow, over a server's labware record.

    `orca.build_system` is the same build over an in-memory labware record.

    Args:
        name: System name.
        topology: Physical layout (locations, transporters, multi-device pools).
        stores: The setup stores. The build attaches each transporter's
            teachpoints and each liquid handler's deck layouts from them.
        workflow: One workflow to register at build time. Servers leave it
            None and register workflows with ``add_workflow_template``.
        labwares: Extra labware templates not owned by any thread (e.g., reservoirs).
        event_bus: The bus the system and its runtime share. `start_runtime` makes one per build.
        description: System description.
        ops_history_store: Where the system records operations. The runtime reads the same store.
        labware_service: The labware record in the database. The placer saves every position to it.
        unsaved_positions: Where a position save goes when it fails after its caller was cancelled.
        config: The system's settings. None reads them from ``stores.system_config()``.
    """
    settings = config if config is not None else stores.system_config()
    _attach_setup_files(topology, stores)
    registry = ResourceRegistry()
    registry.add_resources(list(topology.transporters))
    system_map = SystemMap(registry)

    # Flat model: every device-owned position becomes a site node
    # BEFORE transporter edges wire; the device name never enters the
    # routing graph; it is the reservation mutex key.
    for loc_name, resource in topology.locations.items():
        if isinstance(resource, Device):
            if not registry.has_resource(resource.name):
                registry.add_resource(resource)
            await add_device_sites(system_map, loc_name, resource)
        elif isinstance(resource, SiteRack):
            registry.add_resource(resource)
            for site in resource.sites:
                await system_map.add_location(Location(site.name, site))
        else:
            await system_map.add_location(Location(loc_name, resource))

    await system_map.initialize_transporters()

    for loc_name, resource in topology.locations.items():
        if isinstance(resource, LiquidHandler):
            await add_gripper_edges(registry, system_map, loc_name, resource)

    # Register any remaining location resources (system-location pads)
    for location in system_map.locations:
        resource = location.resource
        if isinstance(resource, IResource) and not registry.has_resource(resource.name):
            registry.add_resource(resource)

    for pool in topology.pools:
        registry.add_resource_pool(pool)

    workflows: List[WorkflowTemplate] = [workflow] if workflow is not None else []
    for wf in workflows:
        for thread_template in wf.thread_templates:
            thread_template.resolve_locations(system_map.resolve_journey_location)

    builder = SdkToSystemBuilder(
        name, description,
        labwares=labwares,
        resources_registry=registry,
        system_map=system_map,
        workflows=workflows,
        event_bus=event_bus,
        config=settings,
        ops_history_store=ops_history_store,
        labware_catalog=stores.labware_catalog(),
        variable_store=await _with_global_variables(stores),
        labware_service=labware_service,
        unsaved_positions=unsaved_positions,
    )
    await builder.bind_labwares()

    _validate_tracked_device_interpreters(topology.locations.values())

    resources = [
        resource for resource in (*topology.locations.values(), *topology.transporters)
        if isinstance(resource, Device | Transporter | SiteRack)
    ]
    return SystemBuild(
        system=builder.get_system(),
        workflow=workflow,
        event_bus=event_bus,
        topology=topology,
        device_assignment=assign_devices(resources, stores.device_setups()),
        config=settings,
    )


async def add_device_sites(system_map: SystemMap, loc_name: str, device: Device) -> None:
    """Create the device's flat site nodes + its off-graph mutex Location.

    The mutex is the action-reservation key (`device.locations == [mutex]`,
    so the resolver reserves the device); each site carries
    the owner back-reference the ownership check and present/stow wire read.
    """
    mutex = Location(loc_name, DeckSite(loc_name))
    system_map.register_mutex_location(mutex)
    system_map.register_device_location(device.name, mutex)
    device.add_location(mutex)

    if isinstance(device, LiquidHandler):
        deck_config = await device.resolve_deck_config_async()
        if deck_config is not None:
            for deck_site_name, _carrier, _idx in enumerate_deck_sites(deck_config):
                node_id = f"{loc_name}/{deck_site_name}"
                deck_site = DeckSiteLocation(
                    node_id, owner=device,
                    resource=DeviceDeckSite(node_id, device),
                    mutex_position_id=loc_name, site_name=deck_site_name,
                )
                await system_map.add_site_location(deck_site)
                device.add_site(deck_site)
            return

    site_ids: list[str] = []
    for site_name in device.site_names:
        site_id = f"{loc_name}/{site_name}"
        bridge = LabwareStagingBridge(site_id, device)
        site = DeckSiteLocation(
            site_id, owner=device, resource=bridge, mutex_position_id=loc_name, site_name=site_name,
        )
        await system_map.add_site_location(site)
        device.add_site(site)
        site_ids.append(site_id)
    system_map.register_teachpoint_alias(loc_name, site_ids)


async def add_gripper_edges(
    registry: ResourceRegistry, system_map: SystemMap, loc_name: str, device: LiquidHandler,
) -> None:
    """Wire the on-deck gripper as transporter edges between ALL deck sites:
    handoff-ness is derived topology (arm-taught sites are the entries), not
    a declaration."""
    deck_config = await device.resolve_deck_config_async()
    if deck_config is None:
        return
    site_nodes = [
        f"{loc_name}/{site_name}"
        for site_name, _carrier, _idx in enumerate_deck_sites(deck_config)
    ]
    if len(site_nodes) < 2:
        return
    gripper = DeckGripperTransporter(device)
    if not registry.has_resource(gripper.name):
        registry.add_resource(gripper)
    device.set_gripper(gripper)
    for i, a in enumerate(site_nodes):
        for b in site_nodes[i + 1:]:
            await system_map.add_edge(a, b, gripper)
            await system_map.add_edge(b, a, gripper)


def _validate_tracked_device_interpreters(resources: Iterable[LocationValue]) -> None:
    """Reject the build if any ITrackedDevice resolves to DefaultInterpreter.

    Called from build_system over topology.locations.values(), which already
    includes both SDK-built local devices and remotely-built devices
    (both register via the topology's locations dict). For every resource
    that implements ITrackedDevice, asserts that ``resolve_operation_interpreter``
    returns a non-default interpreter. The check fires at build time so a
    misconfigured device fails deployment instead of silently dropping
    ops_history records.
    """
    for resource in resources:
        if not isinstance(resource, ITrackedDevice):
            continue
        interpreter = resolve_operation_interpreter(resource)
        if isinstance(interpreter, DefaultInterpreter):
            cls = type(resource).__name__
            iface = next(
                (
                    base.__name__
                    for base in type(resource).__mro__
                    if isinstance(base, type)
                    and base is not ITrackedDevice
                    and issubclass(base, ITrackedDevice)
                    and "operation_interpreter" in base.__dict__
                ),
                "ITrackedDevice",
            )
            raise TrackedDeviceInterpreterError(
                f"Device {getattr(resource, 'name', cls)!r} implements {iface} "
                f"but resolves to DefaultInterpreter; declare "
                f"operation_interpreter() on the interface."
            )


async def add_workflow_template(
    system: ISystem,
    workflow: WorkflowTemplate,
) -> None:
    """Register a workflow template (and its bundled methods + threads) on a built system.

    Resolves the workflow's bundled thread locations against the system's
    location registry, then adds the workflow + its bundled methods +
    bundled threads to the system's template registries. Method and thread
    names are unique per workflow; workflow names are deployment-global.
    Raises ``KeyError`` on duplicate template names. Use this when
    registering a workflow against a long-lived
    ``SystemRuntime`` after the initial ``build_system`` call (e.g.,
    after a workflow file is committed via a hosted submission pipeline).

    Also binds the System's labware catalog onto every labware template
    carried by the workflow's threads AND adds those templates to the
    system's labware registry. Without these steps, templates inside
    dynamically-added workflows raise the ``bind_catalog()`` precondition
    at execution-time materialization, AND operator-facing surfaces
    (``runtime.labware.register(template_name)``) raise KeyError because
    ``SdkToSystemBuilder._derive_labwares`` only runs once at build time
    and sees only the workflows passed to the original ``build_system``.
    """
    for thread_template in workflow.thread_templates:
        thread_template.resolve_locations(system.resolve_journey_location)
    for thread_template in workflow.bundled_threads:
        thread_template.resolve_locations(system.resolve_journey_location)

    catalog = system.labware_catalog
    seen_template_ids: set[int] = set()
    existing_labware_names = {lt.name for lt in system.labware_templates}
    for thread_template in (*workflow.thread_templates, *workflow.bundled_threads):
        labware_template = thread_template.labware_template
        if id(labware_template) in seen_template_ids:
            continue
        await labware_template.bind_catalog(catalog)
        seen_template_ids.add(id(labware_template))
        if labware_template.name not in existing_labware_names:
            system.add_labware_template(labware_template)
            existing_labware_names.add(labware_template.name)

    system.add_workflow_template(workflow)
    if workflow.variable_definitions:
        # Replay the build-time def registration so a workflow added post-build
        # resolves defaults and validates submitted values like a built one.
        system.variable_store.register_workflow_definitions(
            workflow.name, workflow.variable_definitions,
        )
    for method_template in workflow.bundled_methods:
        # Registry keys by (workflow_name, name): same-object re-add is a
        # no-op, a distinct object under the same name in this workflow
        # raises MethodTemplateNameCollisionError.
        if isinstance(method_template, MethodTemplate):
            system.add_method_template(workflow.name, method_template)
    # Same order as SdkToSystemBuilder at build time: wf.start/wf.thread first,
    # then bundled-only threads. A same-object re-add is a no-op.
    for thread_template in (*workflow.thread_templates, *workflow.bundled_threads):
        system.add_labware_thread_template(workflow.name, thread_template)


def compile_workflow_code(
    code: str, topology: "Topology | None" = None,
) -> WorkflowTemplate:
    """Compile a workflow source string into a ``WorkflowTemplate``.

    Used by a hosted submission pipeline when a workflow source string
    arrives via REST and we need to extract the workflow without
    writing the file to disk first. The source must declare a top-level
    ``build_workflow(topology)`` function (the canonical shape a hosted
    submission checks for); this function execs the source in a fresh
    namespace and calls ``build_workflow(topology)`` to construct the
    ``WorkflowTemplate``.

    When ``topology`` is None (test path), a stub topology is passed so
    syntax + decorator capture work but resolve_locations against real
    devices is deferred to the registration call.

    The exec gets a fresh namespace, not a sandbox: the source runs with
    the deployment's own reach. The compiled file lands as
    ``<workflow-injection>``. Raises ``ValueError`` if the source does
    not produce a single ``WorkflowTemplate`` via the expected shape.
    """
    namespace: dict[str, object] = {}
    compiled = compile(code, "<workflow-injection>", "exec")
    exec(compiled, namespace)  # noqa: S102 - the wire contract is "run the operator's own code"

    builder = namespace.get("build_workflow")
    if builder is None or not callable(builder):
        raise ValueError(
            "compile_workflow_code: source must declare a top-level "
            "'build_workflow(topology)' function. The @orca.workflow "
            "decoration runs inside that function's closure."
        )

    effective_topology = topology if topology is not None else _StubTopology()
    result = builder(effective_topology)
    if not isinstance(result, WorkflowTemplate):
        raise ValueError(
            f"compile_workflow_code: build_workflow(topology) must return a "
            f"WorkflowTemplate, got {type(result).__name__}"
        )
    return result


class _StubTopology:
    """Minimal topology stand-in for compile_workflow_code(topology=None).

    Only used by unit tests that compile workflow source without a live
    topology. A production hosted deployment always passes the topology held on
    SystemBuild. The stub raises on attribute access so deployments
    accidentally relying on it surface the misuse loudly.
    """

    def device(self, name: str, kind: object) -> object:
        del name, kind
        return _StubTopology()

    def __getattr__(self, name: str) -> object:
        raise AttributeError(
            f"_StubTopology has no attribute {name!r}; "
            "compile_workflow_code(topology=None) is for source-syntax "
            "validation only. Pass a real topology for full compile."
        )


