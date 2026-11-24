"""A command with no answer flags the labware it names. One that names none flags all at the device.

Two runs share one liquid handler. An aspirate into run A's plate cannot have changed run B's plate, the tip
rack or the trough, so they stay as they were. A discard names no labware, so it is about everything on the deck.
Only a tip command leaves the head unknown: an aspirate cannot have taken a tip on or off.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest

import orca.orca as orca
from cheshire_drivers import CartesianCoordinates as C, Teachpoint
from cheshire_drivers.liquid_handler_models import AspirateRequest, DiscardTipsRequest, LabwareStateResponse
from cheshire_drivers.plr import ChatterboxLiquidHandlerWithProtocolDriver
from cheshire_drivers.shaker_models import ShakeRequest
from cheshire_drivers.sims import SimShakerDriver
from orca.devices.device_interfaces import ILiquidHandler, IShaker
from orca.devices.devices import LiquidHandler, Storage
from orca.devices.shaker import Shaker
from orca.events.event_bus import EventBus
from orca.gateway.operator_recorder import labware_named_by
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.labware import LabwareInitialState
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology, add_workflow_template
from orca.sdk.labware import PlateTemplate, TipRackTemplate
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from orca.state.ops_store import OpsHistorySearchQuery
from orca.state.records import HeadObservationGapDetails, ObservationGapCause, ObservationGapDetails
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.real_device_path import the_real_device_path
from tests.recording_handler import RecordingHandler
from tests.setup_package import package_stores, with_deck_layouts, with_teachpoints, write_deck_layouts, write_teachpoints
from tests.test_deck_resident_reagent_pipetting import ARM_DECK_ENTRY, DECK_CONFIG_PIPETTE

pytestmark = pytest.mark.timeout(120)


class _HandlerWithOneCommandThatNeverAnswers(RecordingHandler):
    def __init__(self, never_answers: str) -> None:
        super().__init__(ChatterboxLiquidHandlerWithProtocolDriver(num_channels=8))
        self._never_answers = never_answers
        self.reached = asyncio.Event()

    async def _hang_if(self, command: str) -> None:
        if command == self._never_answers:
            self.reached.set()
            await asyncio.Event().wait()

    async def aspirate(self, request: AspirateRequest) -> LabwareStateResponse:
        await self._hang_if("aspirate")
        return await super().aspirate(request)

    async def discard_tips(self, request: DiscardTipsRequest) -> LabwareStateResponse:
        await self._hang_if("discard_tips")
        return await super().discard_tips(request)


class _ShakeNeverEnds(SimShakerDriver):
    """Run B's own plate shakes for as long as the test runs, so run B stays a live run."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.shaking = asyncio.Event()

    async def shake(self, request: ShakeRequest) -> None:
        self.shaking.set()
        await asyncio.Event().wait()


@dataclass
class _SharedHandler:
    runtime: SystemRuntime
    run_b: str

    async def stop(self, execution_id: str) -> None:
        """An operator's confirmed stop, which cuts off the command the run's device is running."""
        await asyncio.wait_for(self.runtime.stop_execution(execution_id), timeout=10.0)
        await asyncio.wait_for(self.runtime.stop_execution(execution_id, confirm=True), timeout=20.0)
        await self.runtime.system.command_pipeline.stop_reports_written()

    async def flagged(self) -> set[str]:
        """The template of each labware a no-answer flag stands on, and "head" for the handler's head."""
        found: set[str] = set()
        for labware in self.runtime.system.labwares:
            gaps = [op.details.cause for op in await labware.ops() if isinstance(op.details, ObservationGapDetails)]
            if ObservationGapCause.OPERATIONS_DROPPED in gaps:
                found.add(labware.template_name)
        hits = await self.runtime.system.ops_history.store.search(OpsHistorySearchQuery())
        if any(
            isinstance(op.details, HeadObservationGapDetails) and op.details.device_name == "lh"
            and op.details.cause is ObservationGapCause.OPERATIONS_DROPPED
            for _id, record in hits for op in record.operations
        ):
            found.add("head")
        return found


def _stands_on_the_deck(runtime: SystemRuntime, template_name: str) -> bool:
    return any(labware.template_name == template_name for labware in runtime.system.labware_at_device("lh"))


@asynccontextmanager
async def _two_runs_on_one_handler(never_answers: str) -> AsyncIterator[_SharedHandler]:
    """Run B's plate stands on the deck while run A's command on its own plate gets no answer and is stopped."""
    stores = package_stores()
    driver = _HandlerWithOneCommandThatNeverAnswers(never_answers)
    the_real_device_path().simulate_with("lh", driver)
    plate_a = PlateTemplate(
        "plate_a", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
        initial_state=LabwareInitialState(uniform_volume=100.0),
    )
    plate_b = PlateTemplate(
        "plate_b", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
        initial_state=LabwareInitialState(uniform_volume=100.0),
    )
    carried_b = PlateTemplate(
        "carried_b", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
        initial_state=LabwareInitialState(uniform_volume=100.0),
    )
    trough = PlateTemplate(
        "trough", labware_type="AGenBio_1_troughplate_190000uL_Fl",
        initial_state=LabwareInitialState(uniform_volume=1000.0),
    )
    tips = TipRackTemplate("tips", labware_type="hamilton_96_tiprack_10uL_filter", with_tips=True)
    lh = with_deck_layouts(
        LiquidHandler("lh", deck_layout="default"), write_deck_layouts(stores, "lh", {"default": DECK_CONFIG_PIPETTE}),
    )
    shaker_driver = _ShakeNeverEnds("shaker")
    the_real_device_path().simulate_with("shaker", shaker_driver)
    shaker = Shaker("shaker")
    arm = with_teachpoints(Transporter("arm"), write_teachpoints(stores, "arm", [
        Teachpoint("stacker_b", C(100, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("shaker", C(300, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("stacker_a", C(0, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("pad", C(200, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint(ARM_DECK_ENTRY, C(400, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
    ]))

    @orca.action(device=lh, inputs=[plate_a, tips, trough], deck_positions={plate_a: "carrier-7-0"})
    async def work_on_a(ctx: ActionContext) -> None:
        handler = ctx.device(ILiquidHandler)
        await handler.pick_up_tips(await ctx.next_tips("tips", 1))
        await handler.aspirate([ctx.plate("plate_a").well("A1")], [5.0])
        await handler.discard_tips()

    @orca.method
    async def method_a(ctx: MethodContext):
        yield work_on_a

    @orca.thread(labware=plate_a, start=("stacker_a", DISPENSE), end="waste")
    async def plate_a_journey(ctx: ThreadContext):
        yield method_a

    @orca.thread(labware=tips, start=("lh/carrier-15-0", REUSE_EXISTING), end=("lh/carrier-15-0", LEAVE_IN_PLACE))
    async def tips_journey(ctx: ThreadContext):
        yield orca.join(allows=[method_a])

    @orca.thread(labware=trough, start=("lh/carrier-25-0", REUSE_EXISTING), end=("lh/carrier-25-0", LEAVE_IN_PLACE))
    async def trough_journey(ctx: ThreadContext):
        yield orca.join(allows=[method_a])

    @orca.workflow(name="run_a")
    def workflow_a(wf):
        wf.start(plate_a_journey)
        wf.thread(tips_journey)
        wf.thread(trough_journey)

    @orca.action(device=lh, inputs=[carried_b, plate_b], deck_positions={carried_b: "carrier-7-3"})
    async def work_on_b(ctx: ActionContext) -> None:
        ctx.labware("plate_b")

    @orca.method
    async def method_b(ctx: MethodContext):
        yield work_on_b

    @orca.action(device=shaker, inputs=[carried_b])
    async def shake_b(ctx: ActionContext) -> None:
        await ctx.device(IShaker).shake(duration=1, speed=500)

    @orca.method
    async def shake_method_b(ctx: MethodContext):
        yield shake_b

    @orca.thread(labware=carried_b, start=("stacker_b", DISPENSE), end="waste")
    async def carried_b_journey(ctx: ThreadContext):
        yield method_b
        yield shake_method_b

    @orca.thread(labware=plate_b, start=("lh/carrier-7-1", REUSE_EXISTING), end=("lh/carrier-7-1", LEAVE_IN_PLACE))
    async def plate_b_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[method_b])

    @orca.workflow(name="run_b")
    def workflow_b(wf):
        wf.start(carried_b_journey)
        wf.thread(plate_b_journey)

    topology = Topology(
        locations={
            "stacker_a": Storage("stacker_a"), "stacker_b": Storage("stacker_b"), "pad": PlatePad("pad"), "shaker": shaker,
            "lh": lh, "waste": Storage("waste"),
        },
        transporters=[arm],
    )
    build = await orca.build_system(
        name="Two runs, one handler", workflow=workflow_a, topology=topology, stores=stores,
        event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )
    await add_workflow_template(build.system, workflow_b)
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        run_b = await runtime.submit_workflow("run_b", mode=WorkflowRunMode.CHECK_RUN)
        await asyncio.wait_for(shaker_driver.shaking.wait(), timeout=60.0)
        assert _stands_on_the_deck(runtime, "plate_b")
        run_a = await runtime.submit_workflow("run_a", mode=WorkflowRunMode.CHECK_RUN)
        await asyncio.wait_for(driver.reached.wait(), timeout=60.0)
        shared = _SharedHandler(runtime, run_b.id)
        await shared.stop(run_a.id)
        yield shared
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_an_aspirate_with_no_answer_flags_its_own_plate_only() -> None:
    async with _two_runs_on_one_handler("aspirate") as shared:
        assert await shared.flagged() == {"plate_a"}


@pytest.mark.asyncio
async def test_a_discard_with_no_answer_flags_everything_standing_at_the_handler() -> None:
    async with _two_runs_on_one_handler("discard_tips") as shared:
        assert await shared.flagged() == {"plate_a", "plate_b", "tips", "trough", "head"}


@pytest.mark.asyncio
async def test_a_shake_with_no_answer_flags_the_plate_standing_at_the_shaker() -> None:
    async with _two_runs_on_one_handler("aspirate") as shared:
        await shared.stop(shared.run_b)

        assert await shared.flagged() == {"plate_a", "carried_b"}



_LIQUID_HANDLER = frozenset({"ILiquidHandler"})


def test_an_operators_aspirate_names_its_plate_and_a_discard_names_nothing() -> None:
    aspirate = AspirateRequest.model_validate(
        {"aspirations": [{"labware": "plate_a", "positions": ["A1"], "volumes": [5.0]}]},
    ).model_dump(mode="json")

    assert labware_named_by("lh", "aspirate", aspirate, _LIQUID_HANDLER) == ["plate_a"]
    assert labware_named_by("lh", "discard_tips", {}, _LIQUID_HANDLER) == []
    assert labware_named_by("shaker", "shake", {"speed": 500, "duration": 1}, frozenset({"IShaker"})) == []
