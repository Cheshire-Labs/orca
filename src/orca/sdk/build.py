"""High-level system builder for the Python SDK."""

from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Mapping, Sequence, TypeVar

from orca.events.event_bus import EventBus
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
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.resources import IResource
from orca.resource_models.tracking_interpreter import (
    DefaultInterpreter,
    resolve_operation_interpreter,
)
from orca.resource_models.transporter import Transporter
from orca.state.ops_store import IOpsHistoryStore
from orca.runtime.execution_phase import ExecutionPhase
from orca.runtime.labware_group import LabwareGroup
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.status_models import ExecutionStatus
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.logging_default import install_default_console_logging
from orca.system.deck_sites import enumerate_deck_sites
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.resource_registry import ResourceRegistry
from orca.system.system import System
from orca.system.system_map import SystemMap
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate


class TrackedDeviceInterpreterError(RuntimeError):
    """Raised at build time when an ITrackedDevice resolves to DefaultInterpreter.

    Tracked devices declare ``operation_interpreter()`` via the ITrackedDevice
    contract. Falling through to DefaultInterpreter means actions on this
    device would silently emit only generic records, dropping the typed
    per-command details the interpreter was meant to produce. The build-time
    walk catches this before deployment instead of waiting for ops_history
    to be quietly empty in production.
    """




LogConfig = bool | Callable[[], None]

# Devices and passive placeables are both valid location values; Device is
# not an ILabwarePlaceable on its own -- build_system wraps it in a
# LabwareStagingBridge at runtime. We accept either shape here.
LocationValue = Device | ILabwarePlaceable

_DeviceT = TypeVar("_DeviceT", bound=LocationValue)


@dataclass
class Topology:
    """Physical layout of the lab: locations, transporters, and device pools.

    Separating topology from workflow lets one topology serve many workflows
    (and one workflow run on different topologies, e.g. sim vs hardware).

    Args:
        locations: Named physical positions that can hold labware. Values are
            devices (auto-wrapped in a gateway) or passive pads.
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

    `workflow` is the one workflow `SystemBuild.run()` runs. When `build_system`
    is called without a workflow (multi-workflow deployments), this is None and
    callers drive the system via `SystemRuntime.submit_workflow(name, ...)`
    after registering templates through `runtime.workflow_templates.add(...)`.

    `topology` is the original Topology object passed in. Held on the
    build so post-build workflow registrations (live submissions via
    a hosted deployment's POST /api/workflows) can call build_workflow(topology) to
    resolve device references the same way the cold-boot path does.
    """
    system: System
    workflow: WorkflowTemplate | None
    event_bus: EventBus
    stores: IRuntimeStoreFactory
    topology: "Topology | None" = None

    async def run(
        self,
        run_mode: WorkflowRunMode = WorkflowRunMode.PURE_SIM,
        groups: Sequence[LabwareGroup] = (),
    ) -> ExecutionStatus:
        """Start a runtime, run the workflow once, and shut down. Raises unless the run completes."""
        if self.workflow is None:
            raise RuntimeError(
                "SystemBuild.run() requires a workflow but none was provided "
                "to build_system. For multi-workflow deployments, drive the "
                "system through SystemRuntime.submit_workflow(name) instead."
            )
        runtime = SystemRuntime(self.system, event_bus=self.event_bus)
        try:
            await runtime.start()
            submission = await runtime.submit(self.workflow, groups=groups, mode=run_mode)
            status = await runtime.wait_for_execution(submission)
        finally:
            await runtime.shutdown()
        if status.status is not ExecutionPhase.COMPLETED:
            raise RuntimeError(f"{self.workflow.name} ended {status.status.value}: {status.error}")
        return status


def _apply_log_config(config: LogConfig) -> None:
    if config is False:
        return
    if callable(config):
        config()
        return
    install_default_console_logging()


async def build_system(
    name: str,
    topology: Topology,
    stores: IRuntimeStoreFactory,
    workflow: WorkflowTemplate | None = None,
    labwares: Sequence[LabwareTemplate] | None = None,
    event_bus: IEventBus | None = None,
    description: str = "",
    configure_logging: LogConfig = True,
    ops_history_store: IOpsHistoryStore | None = None,
) -> SystemBuild:
    """Build a runnable system from a workflow and a physical topology.

    This is the main entry point for SDK users. It handles all internal wiring:
    ResourceRegistry, SystemMap, Location objects, and single-device pools.

    Args:
        name: System name.
        topology: Physical layout (locations, transporters, multi-device pools).
        stores: The same runtime store factory passed into ``build_topology``.
            Held on the returned ``SystemBuild`` for downstream consumers
            that need access to the calibration registries.
        workflow: Optional initial workflow. Pass for source-available standalone runs that
            use ``SystemBuild.run()``. Cloud / multi-workflow deployments
            leave this None and register templates through
            ``runtime.workflow_templates.add(...)`` after the system runtime
            is constructed.
        labwares: Extra labware templates not owned by any thread (e.g., reservoirs).
        event_bus: Custom event bus. Created automatically if not provided.
        description: System description.
        configure_logging: Controls SDK logging setup. ``True`` (default) installs
            a stdout INFO handler on the ``orca`` logger if no handlers are
            configured yet. ``False`` leaves logging untouched. A callable is
            invoked for fully custom logging setup.
    """
    _apply_log_config(configure_logging)
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

    bus = event_bus or EventBus()
    builder = SdkToSystemBuilder(
        name, description,
        labwares=labwares,
        resources_registry=registry,
        system_map=system_map,
        workflows=workflows,
        event_bus=bus,
        ops_history_store=ops_history_store,
        labware_catalog=stores.labware_catalog(),
        variable_store=stores.variable_store(),
    )
    await builder.bind_labwares()
    actual_bus = bus if isinstance(bus, EventBus) else EventBus()

    _validate_tracked_device_interpreters(topology.locations.values())

    return SystemBuild(
        system=builder.get_system(),
        workflow=workflow,
        event_bus=actual_bus,
        stores=stores,
        topology=topology,
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
                    mutex_position_id=loc_name,
                )
                await system_map.add_site_location(deck_site)
                device.add_site(deck_site)
            return

    site_ids: list[str] = []
    for site_name in device.site_names:
        site_id = f"{loc_name}/{site_name}"
        bridge = LabwareStagingBridge(site_id, device)
        site = DeckSiteLocation(
            site_id, owner=device, resource=bridge, mutex_position_id=loc_name,
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
    system: System,
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
        # raises MethodTemplateNameCollisionError. Two workflows sharing a
        # method name no longer collide.
        if isinstance(method_template, MethodTemplate):
            system.add_method_template(workflow.name, method_template)
    for thread_template in workflow.bundled_threads:
        # Registry keys by (workflow_name, name): same-object re-add is a
        # no-op, a distinct object under the same name in this workflow
        # raises ThreadTemplateNameCollisionError. Two workflows sharing a
        # thread name no longer collide.
        system.add_labware_thread_template(workflow.name, thread_template)


def compile_workflow_code(
    code: str, topology: "Topology | None" = None,
) -> WorkflowTemplate:
    """Compile a workflow source string into a ``WorkflowTemplate``.

    Used by a hosted submission pipeline when a workflow source string
    arrives via REST/MCP and we need to extract the workflow without
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


