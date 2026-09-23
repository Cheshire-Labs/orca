"""Behavior: ``orca.park("<device>")`` parks on that device's site.

A device name means two different things. To the reservation layer it is the
off-graph mutex that serializes actions on the device; to the router it must be
a real graph node, and the mutex is deliberately not one. Thread
``start=``/``end=`` already resolve through ``resolve_journey_location``, which
maps a device name to its site. Park did not, so it handed the router the mutex
and the move died with "Target <device> cannot be reached from given sources".

Nothing caught it because no example parked on a device: hamilton_smc never calls
``orca.park`` at all, and the harnesses that do park on plate pads, which are
graph nodes already.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import ActionTemplate, IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from tests.test_helpers import (
    execution_outcome,
    create_test_device,
    create_test_transporter,
    wire_system_map,
)


async def _build_park_on_device_system() -> tuple[ISystem, WorkflowTemplate, EventBus]:
    """One plate that parks on ``holding_station`` (a device) mid-journey."""
    station = create_test_device("station")
    holding_station = create_test_device("holding_station")
    transporter = create_test_transporter(
        "robot1", ["start_pad", "station", "holding_station", "waste"],
    )

    plate = PlateTemplate("plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")

    registry = ResourceRegistry()
    registry.add_resource(station)
    registry.add_resource(holding_station)
    registry.add_resource(transporter)
    station_pool = ResourcePool("station", [station])
    registry.add_resource_pool(station_pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map,
        devices={"station": station, "holding_station": holding_station},
        pads=["start_pad", "waste"],
    )

    @orca.action(device=station_pool, inputs=[plate])
    async def agitate(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    async def _agitate_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield agitate
    agitate_method = MethodTemplate("agitate_method", func=_agitate_method)

    start_pad = system_map.get_location("start_pad")
    waste = system_map.get_location("waste")

    async def _plate_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        # The bare device name is the whole point: it must route to the
        # device's site, not to the off-graph reservation mutex.
        yield orca.park("holding_station")
        yield agitate_method
    plate_thread = ThreadTemplate(
        labware_template=plate,
        start=start_pad,
        end=waste,
        func=_plate_thread,
    )

    workflow = WorkflowTemplate("park_on_device_demo")
    workflow.add_thread(plate_thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="park_on_device_system",
        description="",
        labwares=[plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    return builder.get_system(), workflow, event_bus


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_park_on_a_device_name_routes_to_that_devices_site() -> None:
    """A park naming a device completes, and the plate really sits on that
    device's site while parked -- not on the unroutable mutex."""
    system, workflow, event_bus = await _build_park_on_device_system()
    runtime = SystemRuntime(system, event_bus=event_bus)
    await runtime.start()
    try:
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        status = await execution_outcome(runtime, submission, timeout=90.0)
        assert status.status == "completed", (
            "parking on a bare device name must route to its site; routing to the "
            "off-graph mutex fails with NetworkXNoPath"
        )
    finally:
        await runtime.shutdown()


def test_park_target_resolves_to_the_site_not_the_mutex() -> None:
    """The resolution itself: ``holding_station`` gives ``holding_station/slot``.

    ``get_location`` hands back the off-graph mutex for the same name, which is
    correct for reserving and useless for routing.
    """

    async def _check() -> None:
        registry = ResourceRegistry()
        holding_station = create_test_device("holding_station")
        registry.add_resource(holding_station)
        registry.add_resource(
            create_test_transporter("robot1", ["start_pad", "holding_station"])
        )
        system_map = SystemMap(registry)
        await wire_system_map(
            system_map,
            devices={"holding_station": holding_station},
            pads=["start_pad"],
        )

        routed = system_map.resolve_journey_location("holding_station")
        assert routed.position_id == "holding_station/slot"
        assert system_map.location_exists(routed.position_id), (
            "a park target must be a routing node"
        )
        assert not system_map.location_exists(
            system_map.get_location("holding_station").position_id
        ), "the bare device name is the off-graph mutex, never routable"

    asyncio.run(_check())
