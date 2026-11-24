"""A liquid handler call that fails on some channels pauses at DEVICE_OP, like any failed device call.

RETRY sends the whole command once more. It does not re-run the action.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest

import orca.orca as orca
from cheshire_drivers import CartesianCoordinates as C, DeckLayoutConfig, DeckResourceConfig, Teachpoint
from cheshire_drivers.liquid_handler_models import AspirateRequest, ChannelError, LabwareStateResponse
from orca.devices.device_interfaces import ILiquidHandler
from orca.devices.devices import LiquidHandler, Storage
from orca.events.event_bus import EventBus
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.labware import LabwareInitialState
from orca.resource_models.transporter import Transporter
from orca.runtime.execution_record import ExecutionState
from orca.runtime.incident_store import IncidentCategory, RecoveryAction
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
from tests.real_device_path import the_real_device_path
from tests.recording_handler import RecordingHandler
from tests.setup_package import package_stores, with_deck_layouts, with_teachpoints, write_deck_layouts, write_teachpoints
from tests.test_helpers import wait_for_paused_thread

pytestmark = pytest.mark.timeout(120)

DECK = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-15", catalog_ref="TIP_CAR_480_A00", rail=15),
    ],
)
TIPS_SITE = "lh/carrier-15-0"
ARM_DECK_ENTRY = "lh/carrier-7-2"
WORKFLOW = "aspirate_once"


class _FailsOneChannelOnce(RecordingHandler):
    """The first aspirate reports channel 0 failed; every later one works."""

    def __init__(self) -> None:
        super().__init__()
        self.aspirates: list[AspirateRequest] = []

    async def aspirate(self, request: AspirateRequest) -> LabwareStateResponse:
        self.aspirates.append(request)
        answer = await super().aspirate(request)
        if len(self.aspirates) > 1:
            return answer
        target = request.aspirations[0]
        return answer.model_copy(update={"per_channel_errors": [ChannelError(
            channel_id=0, well_position=(target.positions or [None])[0], labware=target.labware,
            attempted_volume=5.0, error_code="clot", error_message="channel 0 clotted",
        )]})


@dataclass
class _Bench:
    runtime: SystemRuntime
    handler: _FailsOneChannelOnce

    async def paused_at_the_aspirate(self) -> tuple[str, ThreadSnapshot]:
        execution_id = (await self.runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.CHECK_RUN)).id
        return execution_id, await wait_for_paused_thread(self.runtime, execution_id, timeout=60.0)


@asynccontextmanager
async def _bench() -> AsyncIterator[_Bench]:
    path = the_real_device_path()
    stores = package_stores()
    handler_driver = _FailsOneChannelOnce()
    path.simulate_with("lh", handler_driver)
    plate = PlateTemplate(
        "plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
        initial_state=LabwareInitialState(uniform_volume=100.0),
    )
    tips = TipRackTemplate("tips", labware_type="hamilton_96_tiprack_10uL_filter", with_tips=True)
    lh = with_deck_layouts(LiquidHandler("lh", deck_layout="default"), write_deck_layouts(stores, "lh", {"default": DECK}))
    arm = with_teachpoints(Transporter("arm"), write_teachpoints(stores, "arm", [
        Teachpoint("stacker", C(0, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint(ARM_DECK_ENTRY, C(400, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
    ]))

    @orca.action(device=lh, inputs=[plate, tips], deck_positions={plate: "carrier-7-0", tips: "carrier-15-0"})
    async def aspirate(ctx: ActionContext) -> None:
        handler = ctx.device(ILiquidHandler)
        await handler.pick_up_tips(await ctx.next_tips("tips", 1))
        await handler.aspirate([ctx.plate("plate").well("A1")], [5.0])
        await handler.discard_tips()

    @orca.method(failure_policy=FailurePolicy.PAUSE)
    async def aspirate_method(ctx: MethodContext):
        yield aspirate

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield aspirate_method

    @orca.thread(labware=tips, start=(TIPS_SITE, REUSE_EXISTING), end=(TIPS_SITE, LEAVE_IN_PLACE))
    async def tips_journey(ctx: ThreadContext):
        yield orca.join(allows=[aspirate_method])

    @orca.workflow(name=WORKFLOW)
    def workflow(wf):
        wf.start(plate_journey)
        wf.thread(tips_journey)

    build = await orca.build_system(
        name="Channel bench", workflow=workflow,
        topology=Topology(locations={"stacker": Storage("stacker"), "lh": lh, "waste": Storage("waste")}, transporters=[arm]),
        stores=stores, event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        yield _Bench(runtime, handler_driver)
    finally:
        await runtime.shutdown()


async def test_a_partial_channel_failure_pauses_at_the_device_call_and_offers_retry_not_retry_action() -> None:
    async with _bench() as bench:
        _execution_id, thread = await bench.paused_at_the_aspirate()

        assert thread.pause_site == "DEVICE_OP"
        assert thread.paused_device_command == "aspirate"
        assert "RETRY" in thread.honoured_decisions
        assert "RETRY_ACTION" not in thread.honoured_decisions


async def test_retry_sends_the_whole_aspirate_once_more_and_the_run_completes() -> None:
    async with _bench() as bench:
        execution_id, thread = await bench.paused_at_the_aspirate()

        await bench.runtime.threads.recover(execution_id, thread.id, RecoveryDecision.RETRY, confirm=True)

        ended = await asyncio.wait_for(bench.runtime.wait(execution_id), timeout=60.0)
        assert ended.status is ExecutionState.COMPLETED
        assert len(bench.handler.aspirates) == 2
        assert bench.handler.aspirates[1] == bench.handler.aspirates[0]
        assert [call.method for call in bench.handler.calls].count("pick_up_tips") == 1


async def test_the_incident_for_a_partial_channel_failure_recommends_retry() -> None:
    async with _bench() as bench:
        execution_id, _thread = await bench.paused_at_the_aspirate()

        [incident] = await bench.runtime.incidents.list(
            execution_id=execution_id, category=IncidentCategory.ACTION_FAILED,
        )
        assert incident.recovery_action is RecoveryAction.THREAD_RECOVER_RETRY
        assert "PartialChannelFailureError" in incident.message
