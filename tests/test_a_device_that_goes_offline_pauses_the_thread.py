"""A device that goes offline during a run pauses the thread for a decision. It never fails the run.

No command is sent a second time when the device's orca-client comes back, whatever the device is.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest

import orca.orca as orca
from cheshire_drivers import CartesianCoordinates as C, DeckLayoutConfig, DeckResourceConfig, Teachpoint
from cheshire_drivers.gateway_protocol import CommandMessage, MessageEnvelope
from cheshire_drivers.transporter_models import PickAtCoordsRequest
from cheshire_drivers.liquid_handler_models import AspirateRequest, LabwareStateResponse
from cheshire_drivers.plr import ChatterboxLiquidHandlerWithProtocolDriver
from cheshire_drivers.shaker_models import ShakeRequest
from cheshire_drivers.sims import SimShakerDriver, SimStorageDriver, SimTransporterDriver
from orca.devices.device_interfaces import ILiquidHandler, IShaker
from orca.devices.devices import LiquidHandler, Storage
from orca.devices.shaker import Shaker
from orca.events.event_bus import EventBus
from orca.gateway.in_process_client import InProcessClient
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.labware import LabwareInitialState
from orca.resource_models.transporter import Transporter
from orca.runtime.execution_record import ExecutionState
from orca.runtime.status_models import ThreadSnapshot
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate, TipRackTemplate
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.status_enums import FailurePolicy, RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.real_device_path import RealDevicePath, the_real_device_path
from tests.recording_handler import RecordingHandler
from tests.setup_package import package_stores, with_deck_layouts, with_teachpoints, write_deck_layouts, write_teachpoints
from tests.test_helpers import run_to_quiescence, wait_for_paused_thread

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

DECK = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-15", catalog_ref="TIP_CAR_480_A00", rail=15),
    ],
)
TIPS_SITE = "lh/carrier-15-0"
ARM_DECK_ENTRY = "lh/carrier-7-2"
WORKFLOW = "shake_then_aspirate"


class _Held:
    """A command the simulator has started and not finished."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    async def hold(self) -> None:
        self.started.set()
        await self.finish.wait()


class _ArmHeldInItsPick(SimTransporterDriver):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.pick = _Held()

    async def pick_at_coords(self, request: PickAtCoordsRequest) -> None:
        await self.pick.hold()
        await super().pick_at_coords(request)


class _StackerHeldInItsDispense(SimStorageDriver):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.dispense_command = _Held()

    async def dispense(self) -> None:
        await self.dispense_command.hold()
        await super().dispense()


class _ShakerHeldInItsShake(SimShakerDriver):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.shake_command = _Held()

    async def shake(self, request: ShakeRequest) -> None:
        await self.shake_command.hold()
        await super().shake(request)


class _HandlerHeldInItsAspirate(RecordingHandler):
    def __init__(self) -> None:
        super().__init__(ChatterboxLiquidHandlerWithProtocolDriver(num_channels=8))
        self.aspirate_command = _Held()

    async def aspirate(self, request: AspirateRequest) -> LabwareStateResponse:
        await self.aspirate_command.hold()
        return await super().aspirate(request)


@dataclass
class _Bench:
    runtime: SystemRuntime
    path: RealDevicePath
    stacker: _StackerHeldInItsDispense
    arm: _ArmHeldInItsPick
    shaker: _ShakerHeldInItsShake
    handler: _HandlerHeldInItsAspirate

    def let_everything_finish(self) -> None:
        for held in (
            self.stacker.dispense_command, self.arm.pick, self.shaker.shake_command, self.handler.aspirate_command,
        ):
            held.finish.set()

    async def run(self) -> str:
        return (await self.runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.CHECK_RUN)).id

    async def drop_and_reconnect(self, device: str) -> None:
        await self.path.drop(device)
        await self.path.come_back(device)

    async def paused_thread(self, execution_id: str) -> ThreadSnapshot:
        return await wait_for_paused_thread(self.runtime, execution_id, timeout=60.0)

    def run_state(self, execution_id: str) -> ExecutionState:
        return self.runtime.get_execution(execution_id).status


@asynccontextmanager
async def _bench(
    failure_policy: FailurePolicy = FailurePolicy.PAUSE, *, hold_the_dispense: bool = False, hold_the_pick: bool = False,
) -> AsyncIterator[_Bench]:
    """A stacker, a shaker, a liquid handler and an arm, each on the real device path, running one plate."""
    path = the_real_device_path()
    stores = package_stores()
    stacker_driver = _StackerHeldInItsDispense("stacker")
    arm_driver = _ArmHeldInItsPick("arm")
    shaker_driver = _ShakerHeldInItsShake("shaker")
    handler_driver = _HandlerHeldInItsAspirate()
    if not hold_the_dispense:
        stacker_driver.dispense_command.finish.set()
    if not hold_the_pick:
        arm_driver.pick.finish.set()
    path.simulate_with("stacker", stacker_driver)
    path.simulate_with("arm", arm_driver)
    path.simulate_with("shaker", shaker_driver)
    path.simulate_with("lh", handler_driver)

    plate = PlateTemplate(
        "plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
        initial_state=LabwareInitialState(uniform_volume=100.0),
    )
    tips = TipRackTemplate("tips", labware_type="hamilton_96_tiprack_10uL_filter", with_tips=True)
    lh = with_deck_layouts(
        LiquidHandler("lh", deck_layout="default"), write_deck_layouts(stores, "lh", {"default": DECK}),
    )
    shaker = Shaker("shaker")
    arm = with_teachpoints(Transporter("arm"), write_teachpoints(stores, "arm", [
        Teachpoint("stacker", C(0, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("shaker", C(200, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint(ARM_DECK_ENTRY, C(400, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
    ]))

    @orca.action(device=shaker, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=500)

    @orca.action(device=lh, inputs=[plate, tips], deck_positions={plate: "carrier-7-0", tips: "carrier-15-0"})
    async def aspirate(ctx: ActionContext) -> None:
        handler = ctx.device(ILiquidHandler)
        await handler.pick_up_tips(await ctx.next_tips("tips", 1))
        await handler.aspirate([ctx.plate("plate").well("A1")], [5.0])
        await handler.discard_tips()

    @orca.method(failure_policy=failure_policy)
    async def shake_method(ctx: MethodContext):
        yield shake

    @orca.method(failure_policy=failure_policy)
    async def aspirate_method(ctx: MethodContext):
        yield aspirate

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield shake_method
        yield aspirate_method

    @orca.thread(labware=tips, start=(TIPS_SITE, REUSE_EXISTING), end=(TIPS_SITE, LEAVE_IN_PLACE))
    async def tips_journey(ctx: ThreadContext):
        yield orca.join(allows=[aspirate_method])

    @orca.workflow(name=WORKFLOW)
    def workflow(wf):
        wf.start(plate_journey)
        wf.thread(tips_journey)

    topology = Topology(
        locations={"stacker": Storage("stacker"), "shaker": shaker, "lh": lh, "waste": Storage("waste")},
        transporters=[arm],
    )
    build = await orca.build_system(
        name="Offline bench", workflow=workflow, topology=topology, stores=stores,
        event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    bench = _Bench(runtime, path, stacker_driver, arm_driver, shaker_driver, handler_driver)
    try:
        yield bench
    finally:
        bench.let_everything_finish()
        await runtime.shutdown()


def _is_waiting_for_a_decision(thread: ThreadSnapshot) -> bool:
    return thread.status == "PAUSED" and thread.pause_reason == "error" and "DeviceOfflineError" in (thread.pause_message or "")


async def test_an_arm_that_drops_during_its_move_pauses_the_thread() -> None:
    async with _bench(hold_the_pick=True) as bench:
        execution_id = await bench.run()
        await asyncio.wait_for(bench.arm.pick.started.wait(), timeout=60.0)

        await bench.drop_and_reconnect("arm")

        thread = await bench.paused_thread(execution_id)
        assert _is_waiting_for_a_decision(thread)
        assert thread.pause_site == "MOVE"
        assert bench.run_state(execution_id) is ExecutionState.RUNNING


async def test_a_shaker_that_drops_during_its_shake_pauses_the_thread() -> None:
    async with _bench() as bench:
        execution_id = await bench.run()
        await asyncio.wait_for(bench.shaker.shake_command.started.wait(), timeout=60.0)

        await bench.drop_and_reconnect("shaker")

        thread = await bench.paused_thread(execution_id)
        assert _is_waiting_for_a_decision(thread)
        assert thread.paused_device_command == "shake"
        assert bench.run_state(execution_id) is ExecutionState.RUNNING


async def test_a_liquid_handler_that_drops_during_its_aspirate_pauses_the_thread() -> None:
    async with _bench() as bench:
        execution_id = await bench.run()
        bench.shaker.shake_command.finish.set()
        await asyncio.wait_for(bench.handler.aspirate_command.started.wait(), timeout=60.0)

        await bench.drop_and_reconnect("lh")

        thread = await bench.paused_thread(execution_id)
        assert _is_waiting_for_a_decision(thread)
        assert thread.paused_device_command == "aspirate"
        assert bench.run_state(execution_id) is ExecutionState.RUNNING


async def test_a_device_that_drops_pauses_the_thread_in_a_method_marked_abort_on_failure() -> None:
    async with _bench(FailurePolicy.ABORT) as bench:
        execution_id = await bench.run()
        await asyncio.wait_for(bench.shaker.shake_command.started.wait(), timeout=60.0)

        await bench.drop_and_reconnect("shaker")

        thread = await bench.paused_thread(execution_id)
        assert _is_waiting_for_a_decision(thread)
        assert "RETRY" in thread.honoured_decisions
        assert bench.run_state(execution_id) is ExecutionState.RUNNING


async def _paused_at_its_start_dispense(bench: _Bench) -> tuple[str, ThreadSnapshot]:
    execution_id = await bench.run()
    await asyncio.wait_for(bench.stacker.dispense_command.started.wait(), timeout=60.0)
    await bench.drop_and_reconnect("stacker")
    return execution_id, await bench.paused_thread(execution_id)


async def test_a_stacker_that_drops_during_the_dispense_at_thread_start_pauses_the_thread() -> None:
    async with _bench(hold_the_dispense=True) as bench:
        execution_id, thread = await _paused_at_its_start_dispense(bench)

        assert _is_waiting_for_a_decision(thread)
        assert thread.pause_site == "THREAD_STEP"
        assert set(thread.honoured_decisions) == {"RETRY", "ABORT_THREAD"}
        assert bench.run_state(execution_id) is ExecutionState.RUNNING
        assert thread.current_location == "stacker/slot"


async def test_a_retry_at_the_start_dispense_dispenses_and_the_thread_carries_on() -> None:
    async with _bench(hold_the_dispense=True) as bench:
        execution_id, thread = await _paused_at_its_start_dispense(bench)
        bench.stacker.dispense_command.finish.set()

        await bench.runtime.threads.recover(execution_id, thread.id, RecoveryDecision.RETRY, confirm=True)

        await asyncio.wait_for(bench.shaker.shake_command.started.wait(), timeout=60.0)


async def test_aborting_the_thread_paused_at_its_start_dispense_ends_it_aborted() -> None:
    async with _bench(hold_the_dispense=True) as bench:
        execution_id, thread = await _paused_at_its_start_dispense(bench)

        await bench.runtime.threads.recover(execution_id, thread.id, RecoveryDecision.ABORT_THREAD, confirm=True)

        statuses = await run_to_quiescence(bench.runtime, execution_id, timeout=60.0)
        assert statuses[thread.name] == "ABORTED"
        assert bench.run_state(execution_id) is not ExecutionState.FAILED
        [aborted] = [t for t in bench.runtime.list_threads(execution_id) if t.id == thread.id]
        assert aborted.current_location == "stacker/slot"


async def test_a_thread_whose_arrived_plate_has_no_position_on_record_still_cannot_be_listed() -> None:
    """Only a plate that never arrived is named at its start. A wiped record of an arrived plate is not guessed at."""
    async with _bench() as bench:
        execution_id = await bench.run()
        await asyncio.wait_for(bench.shaker.shake_command.started.wait(), timeout=60.0)

        bench.runtime.system.labware_location_service.clear_all()

        with pytest.raises(KeyError):
            bench.runtime.list_threads(execution_id)


async def test_a_shake_is_not_sent_again_when_the_shaker_comes_back(monkeypatch: pytest.MonkeyPatch) -> None:
    shakes_sent: list[str] = []
    send = InProcessClient.send_to_client

    async def counting(self: InProcessClient, client_id: str, message: str) -> bool:
        envelope = MessageEnvelope.model_validate_json(message)
        if envelope.type == "command" and CommandMessage.model_validate(envelope.payload).command == "shake":
            shakes_sent.append(message)
        return await send(self, client_id, message)

    monkeypatch.setattr(InProcessClient, "send_to_client", counting)
    async with _bench() as bench:
        execution_id = await bench.run()
        await asyncio.wait_for(bench.shaker.shake_command.started.wait(), timeout=60.0)

        await bench.drop_and_reconnect("shaker")
        await bench.paused_thread(execution_id)

        assert len(shakes_sent) == 1
