"""Shared physical scaffold for the closure e2e family.

The premature-close probe, the crash-fail-open pin, and the feeder-close e2e
all run the same physical world: three stations plus a transporter, the
feeder/mid/pool labware trio (pool shared across groups), and identical
registry / system-map / workflow assembly. That scaffolding lives here once;
each test keeps its actions, methods, and thread generators inline because
those ARE the scenario under test.
"""
import asyncio
from dataclasses import dataclass

from orca.resource_models.labware import PlateTemplate
from orca.resource_models.labware_state import LabwareSlot
from orca.resource_models.location import Location
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import ThreadTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem

from tests.test_helpers import (
    wire_system_map,
    create_test_device,
    create_test_transporter,
)

TERMINAL_STATUSES = {"COMPLETED", "ABORTED", "STOPPED", "FAILED"}

_LABWARE_TYPE = "Cor_Falcon_96_wellplate_340ul_Fb_Black"


@dataclass
class ClosureScaffold:
    """The shared physical world: stations, labware templates, and locations.

    ``*_station_pool`` are the ResourcePools actions run on; ``pool`` is the
    SHARED_ACROSS_GROUPS labware template every scenario converges on.
    """
    registry: ResourceRegistry
    system_map: SystemMap
    feeder: PlateTemplate
    mid: PlateTemplate
    pool: PlateTemplate
    feeder_station_pool: ResourcePool
    mid_station_pool: ResourcePool
    pool_station_pool: ResourcePool
    feeder_pad: Location
    mid_pad: Location
    pool_pad: Location
    waste: Location


async def build_closure_scaffold() -> ClosureScaffold:
    feeder_station = create_test_device("feeder_station", site_names=["site-1", "site-2"])
    mid_station = create_test_device("mid_station", site_names=["site-1", "site-2"])
    pool_station = create_test_device("pool_station", site_names=["site-1", "site-2", "site-3", "site-4"])
    transporter = create_test_transporter(
        "robot1",
        ["feeder_pad", "feeder_station", "mid_pad", "mid_station",
         "pool_pad", "pool_station", "waste"],
    )

    feeder = PlateTemplate("feeder", labware_type=_LABWARE_TYPE)
    mid = PlateTemplate("mid", labware_type=_LABWARE_TYPE)
    pool = PlateTemplate(
        "pool",
        labware_type=_LABWARE_TYPE,
        group_sharing=GroupSharing.SHARED_ACROSS_GROUPS,
        submission_batching=SubmissionBatching.BATCHABLE,
    )

    registry = ResourceRegistry()
    registry.add_resource(feeder_station)
    registry.add_resource(mid_station)
    registry.add_resource(pool_station)
    registry.add_resource(transporter)
    feeder_station_pool = ResourcePool("feeder_station", [feeder_station])
    mid_station_pool = ResourcePool("mid_station", [mid_station])
    pool_station_pool = ResourcePool("pool_station", [pool_station])
    registry.add_resource_pool(feeder_station_pool)
    registry.add_resource_pool(mid_station_pool)
    registry.add_resource_pool(pool_station_pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map,
        devices={
            "feeder_station": feeder_station,
            "mid_station": mid_station,
            "pool_station": pool_station,
        },
        pads=["feeder_pad", "mid_pad", "pool_pad", "waste"],
    )

    return ClosureScaffold(
        registry=registry,
        system_map=system_map,
        feeder=feeder,
        mid=mid,
        pool=pool,
        feeder_station_pool=feeder_station_pool,
        mid_station_pool=mid_station_pool,
        pool_station_pool=pool_station_pool,
        feeder_pad=system_map.get_location("feeder_pad"),
        mid_pad=system_map.get_location("mid_pad"),
        pool_pad=system_map.get_location("pool_pad"),
        waste=system_map.get_location("waste"),
    )


async def finish_closure_system(
    scaffold: ClosureScaffold,
    *,
    workflow_name: str,
    system_name: str,
    feeder_thread: ThreadTemplate,
    mid_thread: ThreadTemplate,
    pool_thread: ThreadTemplate,
) -> tuple[ISystem, WorkflowTemplate, EventBus]:
    workflow = WorkflowTemplate(workflow_name)
    workflow.add_thread(feeder_thread, is_start=True)
    workflow.add_thread(mid_thread)
    workflow.add_thread(pool_thread)
    workflow.register_auto_spawn(mid_thread)
    workflow.register_auto_spawn(pool_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name=system_name,
        description="",
        labwares=[scaffold.feeder, scaffold.mid, scaffold.pool],
        resources_registry=scaffold.registry,
        system_map=scaffold.system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    return builder.get_system(), workflow, event_bus


def feeder_group(gid: str) -> LabwareGroup:
    return LabwareGroup(
        id=gid,
        members=(LabwareGroupMember(thread_template_name="feeder"),),
    )


def pool_slot(runtime: SystemRuntime, eid: str) -> LabwareSlot | None:
    wf = runtime._executions[eid].executing_workflow
    assert wf is not None
    reg = wf._labware_registry
    assert reg is not None
    return next(
        (s for s in reg.all_slots().values() if s.labware_template_name == "pool"),
        None,
    )


async def wait_for_boot(runtime: SystemRuntime, eid: str, timeout: float = 10.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        execution = runtime._executions[eid]
        if execution.executing_workflow is not None:
            return
        await asyncio.sleep(0.02)
    raise RuntimeError("ExecutingWorkflow never attached")
