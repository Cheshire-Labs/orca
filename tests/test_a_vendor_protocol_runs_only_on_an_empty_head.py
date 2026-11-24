"""A vendor protocol runs only on a head the record knows is empty, and leaves the head recorded empty.

Orca cannot see the tips a vendor protocol (a Venus method, a VWorks protocol) takes on or off, so it assumes the
protocol starts and ends with nothing on the head. A head that carries tips, or that nobody has stated, stops the
protocol before it is sent, and RETRY sends it once the head is stated empty.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from pydantic import JsonValue

import orca.orca as orca
from cheshire_drivers import CartesianCoordinates as C, Teachpoint
from cheshire_drivers.protocol_runner_models import RunProtocolRequest
from orca.devices.devices import LiquidHandler, LiquidHandlerProtocol, Storage
from orca.events.event_bus import EventBus
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.device_error import TipsOnTheHeadError
from orca.resource_models.transporter import Transporter
from orca.runtime.sinks import CollectorSink
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.spawn import DISPENSE
from orca.state.mounted import MountedTips
from orca.state.provenance import Provenance
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.status_enums import RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from tests.gateway._operator_command import run_operator_command
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.real_device_path import the_real_device_path
from tests.recording_handler import RecordingHandler
from tests.setup_package import package_stores, with_deck_layouts, with_teachpoints, write_deck_layouts, write_teachpoints
from tests.test_deck_resident_reagent_pipetting import ARM_DECK_ENTRY, DECK_CONFIG_PIPETTE
from tests.test_helpers import run_to_quiescence, wait_for_paused_thread, wait_for_runtime_condition

pytestmark = pytest.mark.timeout(60)

_A_TIP = ("tips", "A1")


class _HandlerWhoseProtocolFails(RecordingHandler):
    async def run_protocol(self, request: RunProtocolRequest) -> None:
        await super().run_protocol(request)
        raise RuntimeError("the method stopped with an error")


@dataclass
class _Bench:
    runtime: SystemRuntime
    driver: RecordingHandler

    def protocols_sent(self) -> int:
        return sum(1 for call in self.driver.calls if call.method == "run_protocol")

    async def head(self) -> MountedTips:
        return await self.runtime.system.mounted_tips.of("lh")

    async def state_the_head(self, by_channel: dict[int, tuple[str, str]]) -> None:
        await self.runtime.system.mounted_tips.assert_mounted("lh", by_channel)

    async def run(self) -> str:
        record = await self.runtime.submit_workflow("protocol_run", mode=WorkflowRunMode.CHECK_RUN)
        return record.id


@asynccontextmanager
async def _bench(driver: RecordingHandler) -> AsyncIterator[_Bench]:
    stores = package_stores()
    the_real_device_path().simulate_with("lh", driver)
    plate = PlateTemplate("plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    lh = with_deck_layouts(
        LiquidHandler("lh", deck_layout="default"), write_deck_layouts(stores, "lh", {"default": DECK_CONFIG_PIPETTE}),
    )
    arm = with_teachpoints(Transporter("arm"), write_teachpoints(stores, "arm", [
        Teachpoint("stacker", C(0, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint(ARM_DECK_ENTRY, C(400, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
    ]))

    @orca.action(device=lh, inputs=[plate], deck_positions={plate: "carrier-7-0"})
    async def vendor_method(ctx: ActionContext) -> None:
        await ctx.device().run_protocol("AddBuffer.hsl", {})

    @orca.method
    async def protocol_method(ctx: MethodContext):
        yield vendor_method

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield protocol_method

    @orca.workflow(name="protocol_run")
    def workflow(wf):
        wf.start(plate_journey)

    topology = Topology(
        locations={"stacker": Storage("stacker"), "lh": lh, "waste": Storage("waste")}, transporters=[arm],
    )
    build = await orca.build_system(
        name="One vendor protocol", workflow=workflow, topology=topology, stores=stores,
        event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        yield _Bench(runtime, driver)
    finally:
        await runtime.shutdown()


def _is_known_empty(head: MountedTips) -> bool:
    return head.provenance is Provenance.KNOWN and not head.by_channel and not head.head96


@pytest.mark.asyncio
async def test_a_protocol_on_a_head_known_empty_runs_and_leaves_it_empty() -> None:
    async with _bench(RecordingHandler()) as bench:
        await bench.state_the_head({})
        run_id = await bench.run()

        assert set((await run_to_quiescence(bench.runtime, run_id)).values()) == {"COMPLETED"}
        assert bench.protocols_sent() == 1
        assert _is_known_empty(await bench.head())


@pytest.mark.asyncio
async def test_a_head_nobody_has_stated_stops_the_protocol_until_it_is_stated_empty() -> None:
    async with _bench(RecordingHandler()) as bench:
        run_id = await bench.run()
        paused = await wait_for_paused_thread(bench.runtime, run_id)

        assert "head" in str(paused.last_error)
        assert bench.protocols_sent() == 0
        await bench.state_the_head({})
        bench.runtime.recover_thread(run_id, paused.id, RecoveryDecision.RETRY)
        assert set((await run_to_quiescence(bench.runtime, run_id)).values()) == {"COMPLETED"}
        assert bench.protocols_sent() == 1


@pytest.mark.asyncio
async def test_tips_on_the_head_stop_the_protocol_until_the_head_is_empty() -> None:
    async with _bench(RecordingHandler()) as bench:
        events = CollectorSink()
        bench.runtime.register_sink(events)
        await bench.state_the_head({0: _A_TIP})
        run_id = await bench.run()
        paused = await wait_for_paused_thread(bench.runtime, run_id)

        def pauses() -> int:
            return sum(1 for event in events.events if event.event_name == f"THREAD.{paused.id}.PAUSED")

        assert "head" in str(paused.last_error)
        assert bench.protocols_sent() == 0
        bench.runtime.recover_thread(run_id, paused.id, RecoveryDecision.RETRY)
        await wait_for_runtime_condition(bench.runtime, lambda: pauses() == 2, message="a second pause")
        assert bench.protocols_sent() == 0, "RETRY with the tips still on stops again"
        await bench.state_the_head({})
        bench.runtime.recover_thread(run_id, paused.id, RecoveryDecision.RETRY)
        assert set((await run_to_quiescence(bench.runtime, run_id)).values()) == {"COMPLETED"}


@pytest.mark.asyncio
async def test_a_failed_protocol_still_leaves_the_head_recorded_empty() -> None:
    async with _bench(_HandlerWhoseProtocolFails()) as bench:
        await bench.state_the_head({})
        run_id = await bench.run()
        await wait_for_paused_thread(bench.runtime, run_id)

        assert bench.protocols_sent() == 1
        assert _is_known_empty(await bench.head())


@pytest.mark.asyncio
async def test_a_protocol_sent_by_hand_is_held_to_the_same_rule() -> None:
    async with _bench(RecordingHandler()) as bench:
        gateway = the_real_device_path().gateway
        params: dict[str, JsonValue] = {"protocol_filepath": "AddBuffer.hsl", "params": {}}

        with pytest.raises(TipsOnTheHeadError):
            await run_operator_command(gateway, bench.runtime, "lh", "run_protocol", params)
        assert bench.protocols_sent() == 0
        await bench.state_the_head({})
        await run_operator_command(gateway, bench.runtime, "lh", "run_protocol", params)
        assert bench.protocols_sent() == 1


@pytest.mark.asyncio
async def test_a_tip_on_the_96_head_stops_the_protocol() -> None:
    async with _bench(RecordingHandler()) as bench:
        await bench.runtime.system.mounted_tips.assert_mounted("lh", {}, {0: _A_TIP})
        run_id = await bench.run()
        paused = await wait_for_paused_thread(bench.runtime, run_id)

        assert "the record says tips are on the head" in str(paused.last_error)
        assert bench.protocols_sent() == 0


@pytest.mark.asyncio
async def test_a_restart_leaves_a_protocol_only_handlers_head_unwatched_and_stops_its_next_protocol() -> None:
    stores = package_stores()
    plate = PlateTemplate("plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    bravo = LiquidHandlerProtocol("bravo", site_names=["site"])
    arm = with_teachpoints(Transporter("arm"), write_teachpoints(stores, "arm", [
        Teachpoint("stacker", C(0, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("bravo/site", C(400, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
    ]))

    @orca.action(device=bravo, inputs=[plate])
    async def vendor_method(ctx: ActionContext) -> None:
        await ctx.device().run_protocol("Wash.pro", {})

    @orca.method
    async def protocol_method(ctx: MethodContext):
        yield vendor_method

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield protocol_method

    @orca.workflow(name="bravo_run")
    def workflow(wf):
        wf.start(plate_journey)

    topology = Topology(
        locations={"stacker": Storage("stacker"), "bravo": bravo, "waste": Storage("waste")}, transporters=[arm],
    )
    build = await orca.build_system(
        name="One protocol-only handler", workflow=workflow, topology=topology, stores=stores,
        event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )
    await build.system.mounted_tips.assert_mounted("bravo", {})
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        assert (await build.system.mounted_tips.of("bravo")).provenance is Provenance.STALE
        assert "bravo" in [subject.subject for subject in await runtime.unsettled_state()]
        record = await runtime.submit_workflow("bravo_run", mode=WorkflowRunMode.CHECK_RUN)
        paused = await wait_for_paused_thread(runtime, record.id)
        assert "nobody has looked at the head since it went unwatched" in str(paused.last_error)
    finally:
        await runtime.shutdown()
