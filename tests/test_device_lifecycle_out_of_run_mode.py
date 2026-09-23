"""An out-of-run device lifecycle verb means the REAL device by default.

The gap this pins (D4, ruling 4 of the state-reconciliation rulings): facade
device verbs resolved their driver off the ambient run-mode ContextVar, which
outside an execution falls back to PURE_SIM. So `operations_initialize_device`
initialized the in-process simulator and reported success -- it never reached
an instrument. Ruling: out-of-run device endpoints default to LIVE, with an
optional per-request mode parameter, and the topology sim_override ratchet is
unchanged (a device declared sim stays off hardware whatever the caller says).
"""

from collections.abc import AsyncGenerator

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.registries import NullGatewayRegistry
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.mock import UniversalMockDevice, UniversalSimDriver
from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    wire_system_map,
)


class _TrackingDriver(UniversalSimDriver):
    """Records which verbs landed on this driver slot."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.calls: list[str] = []

    async def initialize(self) -> None:
        self.calls.append("initialize")
        await super().initialize()

    async def connect(self) -> None:
        self.calls.append("connect")
        await super().connect()

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        await super().disconnect()

    async def execute(self, command: str, options: dict) -> None:
        self.calls.append(f"execute:{command}")
        await super().execute(command, options)

    async def shake(self, request) -> None:
        self.calls.append("shake")
        await super().shake(request)


async def _build_runtime(
    *, sim_override: WorkflowRunMode | None = None,
) -> tuple[SystemRuntime, _TrackingDriver, _TrackingDriver]:
    live = _TrackingDriver("shaker1-live")
    sim = _TrackingDriver("shaker1-sim")
    device = UniversalMockDevice(
        "shaker1", driver=live, sim_driver=sim, sim_override=sim_override,
    )
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_oor_mode")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake_action

    pad_loc = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
    async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        del ctx
        yield shake_method

    workflow = WorkflowTemplate("wf_oor_mode")
    workflow.add_thread(plate_thread, is_start=True)

    builder = SdkToSystemBuilder(
        name="oor_mode_system",
        description="",
        labwares=[plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=EventBus(),
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = SystemRuntime(system, gateway_registry=NullGatewayRegistry())
    return runtime, live, sim


async def test_initialize_out_of_run_reaches_the_live_driver() -> None:
    runtime, live, sim = await _build_runtime()

    await runtime.devices.initialize("shaker1", confirm=True)

    assert live.calls == ["initialize"]
    assert sim.calls == []


async def test_mode_param_keeps_the_verb_on_the_simulator() -> None:
    runtime, live, sim = await _build_runtime()

    await runtime.devices.initialize(
        "shaker1", mode=WorkflowRunMode.PURE_SIM, confirm=True,
    )

    assert sim.calls == ["initialize"]
    assert live.calls == []


async def test_declared_sim_device_stays_off_the_live_driver() -> None:
    """The sim_override ratchet is unchanged: a device the topology declares
    sim never reaches hardware, whatever base the caller supplies."""
    runtime, live, sim = await _build_runtime(
        sim_override=WorkflowRunMode.PURE_SIM,
    )

    await runtime.devices.initialize("shaker1", confirm=True)

    assert sim.calls == ["initialize"]
    assert live.calls == []


async def test_connect_and_disconnect_share_the_live_default() -> None:
    runtime, live, sim = await _build_runtime()

    await runtime.devices.connect("shaker1")
    await runtime.devices.disconnect("shaker1", confirm=True)

    assert live.calls == ["connect", "disconnect"]
    assert sim.calls == []


async def test_execute_and_invoke_reach_the_live_driver() -> None:
    """The ad-hoc command escape hatches are device WRITES too: out of a run
    `orca device send/invoke` used to dispense from the simulator."""
    runtime, live, sim = await _build_runtime()

    await runtime.devices.execute("shaker1", "beep", confirm=True)
    await runtime.devices.invoke(
        "shaker1", "shake", {"duration": 1, "speed": 500}, confirm=True,
    )

    assert live.calls == ["execute:beep", "shake"]
    assert sim.calls == []


async def test_execute_mode_param_keeps_the_command_on_the_simulator() -> None:
    runtime, live, sim = await _build_runtime()

    await runtime.devices.execute(
        "shaker1", "beep", mode=WorkflowRunMode.PURE_SIM, confirm=True,
    )

    assert sim.calls == ["execute:beep"]
    assert live.calls == []


async def test_snapshot_mode_and_flags_describe_the_same_world() -> None:
    """The blocker shape: initialize drives the LIVE slot and is_initialized
    reads it back, so the snapshot's effective_mode must resolve the same
    write base -- not the ambient out-of-run fallback -- or one response mixes
    two worlds (is_initialized=True from LIVE next to effective_mode=PURE_SIM)."""
    runtime, live, sim = await _build_runtime()

    await runtime.devices.initialize("shaker1", confirm=True)
    snapshot = runtime.devices.get_device_status("shaker1")

    assert live.calls == ["initialize"]
    assert snapshot.is_initialized is True
    assert snapshot.effective_mode is WorkflowRunMode.LIVE


async def test_declared_sim_snapshot_stays_in_the_sim_world() -> None:
    """The ratchet side of the same coherence: a topology-declared sim device
    reports the sim mode next to the sim slot's flags."""
    runtime, live, sim = await _build_runtime(
        sim_override=WorkflowRunMode.PURE_SIM,
    )

    await runtime.devices.initialize("shaker1", confirm=True)
    snapshot = runtime.devices.get_device_status("shaker1")

    assert sim.calls == ["initialize"]
    assert snapshot.effective_mode is WorkflowRunMode.PURE_SIM
