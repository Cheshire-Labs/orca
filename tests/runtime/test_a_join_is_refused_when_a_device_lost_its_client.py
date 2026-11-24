"""A submission joining a running run is refused at its start, like a fresh run, when a device lost its orca-client."""

import asyncio

import pytest

from orca.events.event_bus import EventBus
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resource_pool import ResourcePool
from orca.modes.device_modes import DeviceMode
from orca.workflow_models.labware_group import LabwareGroup, LabwareGroupMember
from orca.modes.run_modes import WorkflowRunMode
from orca.workflow_models.submission import BatchMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology, compile_workflow_code
from orca.sdk.package import build_system
from orca.system.errors import DevicesNotReadyError
from tests.no_devices import NullGatewayRegistry
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.runtime.manual_place_fixtures import _live_connection_source
from tests.runtime.test_an_offline_live_device_refuses_a_run_at_its_start import _device, _until, _workflow_source
from tests.device_modes import run_by_an_orca_client
from tests.setup_package import package_stores
from tests.test_helpers import create_test_transporter


async def _runtime_with_a_running_sim_run(prefix: str) -> tuple[SystemRuntime, str, asyncio.Event, str]:
    shaker, driver = _device(f"{prefix}_a", powered=True, setting=DeviceMode.SIM)
    driver.hold = asyncio.Event()
    run_by_an_orca_client(shaker)
    transporter = create_test_transporter("robot1", [shaker.name, "pad1"])
    topology = Topology(
        locations={shaker.name: shaker, "pad1": PlatePad("pad1")},
        transporters=[transporter],
        pools=[ResourcePool("shakers", [shaker])],
    )
    template = compile_workflow_code(_workflow_source(prefix, ["pad1"]), topology)
    build = await build_system(
        prefix, topology, package_stores(), workflow=template, event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )
    connections = _live_connection_source((shaker.name,))
    runtime = await memory_runtime(
        build.system, event_bus=build.event_bus, gateway_registry=NullGatewayRegistry(), connection_source=connections,
    )
    await runtime.start()
    group = LabwareGroup(id="g1", members=(LabwareGroupMember(thread_template_name=f"{prefix}_pad1_journey"),))
    await runtime.submit(template, groups=[group], mode=WorkflowRunMode.SIM, batch_mode=BatchMode.STANDALONE)
    await _until(lambda: driver.shakes == 1)
    connections._by_name.pop(shaker.name)
    return runtime, template.name, driver.hold, shaker.name


async def test_a_join_is_refused_when_a_device_of_the_run_lost_its_client() -> None:
    runtime, workflow_name, hold, lost = await _runtime_with_a_running_sim_run("join_lost")
    try:
        workflow = runtime.system.get_workflow_template(workflow_name)
        group = LabwareGroup(id="g2", members=(LabwareGroupMember(thread_template_name="join_lost_pad1_journey"),))

        with pytest.raises(DevicesNotReadyError) as refused:
            await runtime.submit(workflow, groups=[group], mode=WorkflowRunMode.SIM, batch_mode=BatchMode.JOIN_EXISTING)

        assert list(refused.value.devices) == [lost]
    finally:
        hold.set()
        await runtime.shutdown()
