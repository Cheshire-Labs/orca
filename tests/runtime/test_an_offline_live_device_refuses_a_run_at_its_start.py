"""A run is refused at its start if a device it uses is set to live and is offline; a pool needs one device."""

import asyncio
from collections.abc import Callable

import pytest

from tests.device_modes import run_by_an_orca_client
from cheshire_drivers.shaker_models import ShakeRequest

from orca.events.event_bus import EventBus
from orca.gateway.controller.controller import DeviceController
from orca.gateway.device_fault import DeviceFaultOutcome
from orca.resource_models.devices import Device
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resource_pool import ResourcePool
from orca.modes.device_modes import DeviceMode
from orca.runtime.incident_store import DeviceInitFailedDetail, IncidentCategory, IncidentSeverity
from orca.modes.run_modes import WorkflowRunMode, mode_scope
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.build import Topology, compile_workflow_code
from orca.sdk.package import build_system
from orca.system.errors import DevicesNotReadyError
from tests.device_modes import InMemoryDeviceModes
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.mock import UniversalMockDevice, UniversalSimDriver
from tests.runtime.manual_place_fixtures import _live_connection_source
from tests.setup_package import package_stores
from tests.test_helpers import create_test_transporter, wait_until
from tests.no_devices import NullGatewayRegistry


async def _fault(runtime: SystemRuntime, device_name: str) -> None:
    """An arm-style fault: the last command got no answer, so the device may still hold a plate."""
    controller = runtime._device_controller
    assert isinstance(controller, DeviceController)
    await controller._latch_fault(
        device_name, "shake", "c-1", TimeoutError("no answer"), DeviceFaultOutcome.UNKNOWN, None,
    )


class _Instrument(UniversalSimDriver):
    """A real instrument: powered off, it fails to come up live; its simulator always comes up."""

    def __init__(self, name: str, *, powered: bool) -> None:
        super().__init__(name)
        self.powered = powered
        self.initializes = 0
        self.device: Device[UniversalSimDriver] | None = None
        self.shakes = 0
        self.hold: asyncio.Event | None = None

    async def initialize(self) -> None:
        assert self.device is not None
        self.initializes += 1
        if not self.powered and await self.device.effective_mode() is WorkflowRunMode.LIVE:
            raise ConnectionError(f"{self.name} does not answer")
        await super().initialize()

    async def shake(self, request: ShakeRequest) -> None:
        self.shakes += 1
        if self.hold is not None:
            await self.hold.wait()
        await super().shake(request)


async def _until(condition: Callable[[], bool]) -> None:
    """A live run ends waiting for an operator to unload, so these tests watch the work rather than the run's end."""
    await wait_until(condition, timeout=10.0)


def _device(name: str, *, powered: bool, setting: DeviceMode) -> tuple[UniversalMockDevice, _Instrument]:
    driver = _Instrument(name, powered=powered)
    device = UniversalMockDevice(name, driver=driver)
    driver.device = device
    device.use_device_modes(InMemoryDeviceModes({name: setting}))
    return device, driver


def _workflow_source(prefix: str, pads: list[str]) -> str:
    """One plate per pad, each shaken once on the pool."""
    lines = [
        "import orca.orca as orca",
        "from tests.test_helpers import create_test_plate_template",
        "",
        "def build_workflow(topology):",
        "    shakers = topology.pool('shakers')",
    ]
    for pad in pads:
        lines += [
            f"    {prefix}_{pad}_plate = create_test_plate_template('{prefix}_{pad}')",
            f"    @orca.action(device=shakers, inputs=[{prefix}_{pad}_plate])",
            f"    async def {prefix}_{pad}_shake(ctx):",
            "        await ctx.device().shake(duration=1, speed=1000)",
            "    @orca.method",
            f"    async def {prefix}_{pad}_method(ctx):",
            f"        yield {prefix}_{pad}_shake",
            f"    @orca.thread(labware={prefix}_{pad}_plate, start='{pad}', end='{pad}')",
            f"    async def {prefix}_{pad}_journey(ctx):",
            f"        yield {prefix}_{pad}_method",
        ]
    lines += [f"    @orca.workflow(name='{prefix}_wf')", f"    def {prefix}_wf(w):"]
    lines += [f"        w.start({prefix}_{pad}_journey)" for pad in pads]
    lines += [f"    return {prefix}_wf", ""]
    return "\n".join(lines)


async def _runtime(
    prefix: str, devices: list[UniversalMockDevice], pads: list[str],
    *, more_pools: list[ResourcePool] = [], connected: list[str] | None = None,
) -> tuple[SystemRuntime, str]:
    transporter = create_test_transporter("robot1", [*(d.name for d in devices), *pads])
    topology = Topology(
        locations={**{d.name: d for d in devices}, **{p: PlatePad(p) for p in pads}},
        transporters=[transporter],
        pools=[ResourcePool("shakers", devices), *more_pools],
    )
    template = compile_workflow_code(_workflow_source(prefix, pads), topology)
    build = await build_system(prefix, topology, package_stores(), workflow=template, event_bus=EventBus(), ops_history_store=memory_ops_store())
    runtime = await memory_runtime(
        build.system, event_bus=build.event_bus, gateway_registry=NullGatewayRegistry(),
        connection_source=_live_connection_source(tuple(connected if connected is not None else (d.name for d in devices))),
    )
    await runtime.start()
    # A live run starts from plates an operator put down.
    for pad in pads:
        await runtime.labware.register(f"{prefix}_{pad}", location=pad, confirm=True)
    return runtime, template.name


async def test_a_live_run_is_refused_at_its_start_when_a_device_set_to_live_is_offline() -> None:
    shaker, driver = _device("off_live_a", powered=False, setting=DeviceMode.LIVE)
    runtime, workflow = await _runtime("off_live", [shaker], ["pad1"])
    try:
        with pytest.raises(DevicesNotReadyError) as refused:
            await runtime.submit_workflow(workflow, mode=WorkflowRunMode.LIVE)

        assert list(refused.value.devices) == ["off_live_a"]
        assert "does not answer" in str(refused.value)
        assert runtime.list_executions() == []
        assert driver.shakes == 0
    finally:
        await runtime.shutdown()


async def test_an_offline_device_set_to_sim_does_not_refuse_a_live_run() -> None:
    shaker, driver = _device("off_sim_a", powered=False, setting=DeviceMode.SIM)
    runtime, workflow = await _runtime("off_sim", [shaker], ["pad1"])
    try:
        await runtime.submit_workflow(workflow, mode=WorkflowRunMode.LIVE)

        await _until(lambda: driver.shakes == 1)
    finally:
        await runtime.shutdown()


async def test_a_pool_with_no_device_available_refuses_the_run() -> None:
    first, _ = _device("pool_none_a", powered=False, setting=DeviceMode.LIVE)
    second, _ = _device("pool_none_b", powered=False, setting=DeviceMode.LIVE)
    runtime, workflow = await _runtime("pool_none", [first, second], ["pad1"])
    try:
        with pytest.raises(DevicesNotReadyError) as refused:
            await runtime.submit_workflow(workflow, mode=WorkflowRunMode.LIVE)

        assert sorted(refused.value.devices) == ["pool_none_a", "pool_none_b"]
        assert runtime.list_executions() == []
    finally:
        await runtime.shutdown()


async def test_a_running_pool_whose_only_free_device_is_offline_waits_for_a_busy_one() -> None:
    offline, offline_driver = _device("pool_wait_a", powered=False, setting=DeviceMode.LIVE)
    online, online_driver = _device("pool_wait_b", powered=True, setting=DeviceMode.LIVE)
    online_driver.hold = asyncio.Event()
    runtime, workflow = await _runtime("pool_wait", [offline, online], ["pad1", "pad2"])
    try:
        record = await runtime.submit_workflow(workflow, mode=WorkflowRunMode.LIVE)
        await _until(lambda: online_driver.shakes == 1)
        # One plate shakes on the online device; the other could take the offline one, and must wait instead.
        await _until(lambda: [t.status for t in runtime.list_threads(record.id)].count(
            "AWAITING_ACTION_RESERVATION") == 1)
        assert offline_driver.shakes == 0
        assert runtime.get_execution(record.id).status is ExecutionState.RUNNING

        online_driver.hold.set()

        await _until(lambda: online_driver.shakes == 2)
        assert offline_driver.shakes == 0
        assert runtime.get_execution(record.id).status is ExecutionState.RUNNING
    finally:
        await runtime.shutdown()


async def test_a_device_whose_orca_client_is_not_connected_refuses_the_run_the_same_way() -> None:
    shaker, driver = _device("no_client_a", powered=True, setting=DeviceMode.LIVE)
    run_by_an_orca_client(shaker)
    runtime, workflow = await _runtime("no_client", [shaker], ["pad1"], connected=[])
    try:
        with pytest.raises(DevicesNotReadyError) as refused:
            await runtime.submit_workflow(workflow, mode=WorkflowRunMode.LIVE)

        assert "orca-client is not connected" in refused.value.devices["no_client_a"]
        assert driver.shakes == 0
    finally:
        await runtime.shutdown()


async def test_overlapping_pools_refuse_the_run_when_one_of_them_has_no_device_up() -> None:
    a, _ = _device("overlap_a", powered=False, setting=DeviceMode.LIVE)
    b, _ = _device("overlap_b", powered=False, setting=DeviceMode.LIVE)
    c, _ = _device("overlap_c", powered=True, setting=DeviceMode.LIVE)
    d, _ = _device("overlap_d", powered=True, setting=DeviceMode.LIVE)
    # Each of a and b has a pool with a device up, but the pool of a and b has none.
    runtime, workflow = await _runtime(
        "overlap", [a, b], ["pad1"],
        more_pools=[ResourcePool("ac", [a, c]), ResourcePool("bd", [b, d])],
        connected=["overlap_a", "overlap_b", "overlap_c", "overlap_d"],
    )
    try:
        with pytest.raises(DevicesNotReadyError) as refused:
            await runtime.submit_workflow(workflow, mode=WorkflowRunMode.LIVE)

        assert sorted(refused.value.devices) == ["overlap_a", "overlap_b"]
    finally:
        await runtime.shutdown()


async def test_an_offline_pool_member_is_tried_again_at_the_next_runs_start() -> None:
    offline, offline_driver = _device("retry_a", powered=False, setting=DeviceMode.LIVE)
    online, _ = _device("retry_b", powered=True, setting=DeviceMode.LIVE)
    runtime, workflow = await _runtime("retry", [offline, online], ["pad1"])
    try:
        await runtime.submit_workflow(workflow, mode=WorkflowRunMode.LIVE)
        assert offline.is_offline
        warnings = await runtime.incidents.list(category=IncidentCategory.DEVICE_INIT_FAILED)
        assert [(i.detail, i.severity) for i in warnings] == [
            (DeviceInitFailedDetail(device_name="retry_a", driver_error="retry_a does not answer"), IncidentSeverity.WARNING),
        ]

        offline_driver.powered = True
        with mode_scope(WorkflowRunMode.LIVE):
            assert await runtime.system.bring_up_for_a_run({}) == []

        assert not offline.is_offline
    finally:
        await runtime.shutdown()


async def test_a_faulted_device_refuses_a_run_at_its_start_and_keeps_its_fault() -> None:
    """Only an operator who looked, or a recovery decision, clears a fault; a run's bring-up must not."""
    shaker, driver = _device("faulted_a", powered=True, setting=DeviceMode.LIVE)
    runtime, workflow = await _runtime("faulted", [shaker], ["pad1"])
    try:
        await _fault(runtime, "faulted_a")

        with pytest.raises(DevicesNotReadyError) as refused:
            await runtime.submit_workflow(workflow, mode=WorkflowRunMode.LIVE)

        assert list(refused.value.devices) == ["faulted_a"]
        assert "no answer" in refused.value.devices["faulted_a"]
        assert driver.initializes == 0
        assert runtime.device_faults.fault("faulted_a") is not None
        assert runtime.list_executions() == []
    finally:
        await runtime.shutdown()


async def test_a_faulted_pool_member_is_left_out_and_the_other_runs_the_work() -> None:
    faulted, faulted_driver = _device("faulted_pool_a", powered=True, setting=DeviceMode.LIVE)
    healthy, healthy_driver = _device("faulted_pool_b", powered=True, setting=DeviceMode.LIVE)
    runtime, workflow = await _runtime("faulted_pool", [faulted, healthy], ["pad1"])
    try:
        await _fault(runtime, "faulted_pool_a")

        await runtime.submit_workflow(workflow, mode=WorkflowRunMode.LIVE)

        await _until(lambda: healthy_driver.shakes == 1)
        assert faulted_driver.initializes == 0
        assert faulted_driver.shakes == 0
        assert runtime.device_faults.fault("faulted_pool_a") is not None
    finally:
        await runtime.shutdown()


async def test_once_the_fault_is_cleared_the_next_run_brings_the_device_up() -> None:
    shaker, driver = _device("cleared_a", powered=True, setting=DeviceMode.LIVE)
    runtime, workflow = await _runtime("cleared", [shaker], ["pad1"])
    try:
        await _fault(runtime, "cleared_a")
        with pytest.raises(DevicesNotReadyError):
            await runtime.submit_workflow(workflow, mode=WorkflowRunMode.LIVE)

        await runtime.device_faults.clear_fault("cleared_a")
        await runtime.submit_workflow(workflow, mode=WorkflowRunMode.LIVE)

        await _until(lambda: driver.shakes == 1)
        assert driver.initializes == 1
    finally:
        await runtime.shutdown()
