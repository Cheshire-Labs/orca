"""A run's command the device failed after taking it: the record is flagged, and settling its incident later is safe.

The incident keeps what the command stated. A person may settle it long after the run has retried and moved on, so
mark complete applies the stored move only while the labware is still where the command started from.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest

import orca.orca as orca
from cheshire_drivers import CartesianCoordinates as C, DeckLayoutConfig, DeckResourceConfig, Teachpoint
from cheshire_drivers.liquid_handler_models import AspirateRequest, LabwareStateResponse
from cheshire_drivers.plr import ChatterboxLiquidHandlerWithProtocolDriver
from cheshire_drivers.sims import SimTransporterDriver
from cheshire_drivers.transporter_models import PickAtCoordsRequest
from orca.devices.device_interfaces import ILiquidHandler
from orca.devices.devices import LiquidHandler, Storage
from orca.events.event_bus import EventBus
from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.labware import LabwareInitialState, LabwareInstance
from orca.resource_models.transporter import Transporter
from orca.runtime.incident_store import IncidentCategory, SystemIncident
from orca.runtime.status_models import ThreadSnapshot
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate, TipRackTemplate
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from orca.state.ops_store import OpsHistorySearchQuery
from orca.state.records import HeadObservationGapDetails, ObservationGapCause, ObservationGapDetails
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.status_enums import RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.real_device_path import the_real_device_path
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
PLATE_SITE = "lh/carrier-7-0"
ARM_DECK_ENTRY = "lh/carrier-7-2"
WORKFLOW = "aspirate_from_a_plate"


class _ArmWhoseFirstPickFails(SimTransporterDriver):
    """The arm takes its first pick and then reports a failure. Later picks work."""

    def __init__(self, name: str, fails: bool) -> None:
        super().__init__(name)
        self._fails = fails

    async def pick_at_coords(self, request: PickAtCoordsRequest) -> None:
        if self._fails:
            self._fails = False
            raise RuntimeError("gripper stalled part-way")
        await super().pick_at_coords(request)


class _HandlerAtItsAspirate(RecordingHandler):
    """The aspirate either fails after the handler took it, or holds until the test lets it finish."""

    def __init__(self, fails: bool) -> None:
        super().__init__(ChatterboxLiquidHandlerWithProtocolDriver(num_channels=8))
        self._fails = fails
        self.reached = asyncio.Event()
        self.finish = asyncio.Event()

    async def aspirate(self, request: AspirateRequest) -> LabwareStateResponse:
        if self._fails:
            raise RuntimeError("pressure error on channel 1")
        self.reached.set()
        await self.finish.wait()
        return await super().aspirate(request)


@dataclass
class _Bench:
    runtime: SystemRuntime
    handler: _HandlerAtItsAspirate

    async def run(self) -> str:
        return (await self.runtime.submit_workflow(WORKFLOW, mode=WorkflowRunMode.CHECK_RUN)).id

    async def paused_thread(self, execution_id: str) -> ThreadSnapshot:
        return await wait_for_paused_thread(self.runtime, execution_id, timeout=60.0)

    def plate(self) -> LabwareInstance:
        return next(labware for labware in self.runtime.system.labwares if labware.template_name == "plate")

    def plate_site(self) -> str:
        return self.runtime.system.labware_location_service.get(self.plate()).position_id

    async def outcome_unknown_incidents(self) -> list[SystemIncident]:
        return await self.runtime.incidents.list(category=IncidentCategory.RECOVERABLE_TIMEOUT)

    async def flagged(self) -> set[str]:
        """The template of each labware a cut-off flag stands on, and "head" for the handler's head."""
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


@asynccontextmanager
async def _bench(
    *, pick_fails: bool = False, aspirate_fails: bool = False, ends_at: str = "waste",
) -> AsyncIterator[_Bench]:
    """A stacker, a liquid handler and an arm on the real device path, running one plate to the deck and back."""
    path = the_real_device_path()
    stores = package_stores()
    handler_driver = _HandlerAtItsAspirate(aspirate_fails)
    path.simulate_with("arm", _ArmWhoseFirstPickFails("arm", pick_fails))
    path.simulate_with("lh", handler_driver)

    plate = PlateTemplate(
        "plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
        initial_state=LabwareInitialState(uniform_volume=100.0),
    )
    tips = TipRackTemplate("tips", labware_type="hamilton_96_tiprack_10uL_filter", with_tips=True)
    lh = with_deck_layouts(
        LiquidHandler("lh", deck_layout="default"), write_deck_layouts(stores, "lh", {"default": DECK}),
    )
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

    @orca.method
    async def aspirate_method(ctx: MethodContext):
        yield aspirate

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end=(ends_at, LEAVE_IN_PLACE))
    async def plate_journey(ctx: ThreadContext):
        yield aspirate_method

    @orca.thread(labware=tips, start=(TIPS_SITE, REUSE_EXISTING), end=(TIPS_SITE, LEAVE_IN_PLACE))
    async def tips_journey(ctx: ThreadContext):
        yield orca.join(allows=[aspirate_method])

    @orca.workflow(name=WORKFLOW)
    def workflow(wf):
        wf.start(plate_journey)
        wf.thread(tips_journey)

    topology = Topology(
        locations={"stacker": Storage("stacker"), "lh": lh, "waste": Storage("waste")}, transporters=[arm],
    )
    build = await orca.build_system(
        name="Failed command bench", workflow=workflow, topology=topology, stores=stores,
        event_bus=EventBus(), ops_history_store=memory_ops_store(),
    )
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        yield _Bench(runtime, handler_driver)
    finally:
        handler_driver.finish.set()
        await runtime.shutdown()


async def test_an_aspirate_that_fails_inside_a_run_flags_its_plate_only() -> None:
    async with _bench(aspirate_fails=True) as bench:
        execution_id = await bench.run()

        thread = await bench.paused_thread(execution_id)

        assert thread.paused_device_command == "aspirate"
        assert await bench.flagged() == {"plate"}, "the tip rack was not named, and an aspirate takes no tip on or off"
        incidents = await bench.outcome_unknown_incidents()
        assert len(incidents) == 1
        assert "'aspirate' on 'lh' failed after the device took it" in incidents[0].message


async def test_mark_complete_after_the_run_retried_and_moved_on_does_not_replay_the_pick() -> None:
    """The arm's pick fails, a person retries, and the plate is carried to the deck. Settling the old incident then
    must not put the plate back in the jaws."""
    async with _bench(pick_fails=True) as bench:
        execution_id = await bench.run()
        thread = await bench.paused_thread(execution_id)
        assert thread.pause_site == "MOVE"
        [incident] = await bench.outcome_unknown_incidents()
        assert "'pick_at_coords' on 'arm' failed after the device took it" in incident.message

        await bench.runtime.threads.recover(execution_id, thread.id, RecoveryDecision.RETRY, confirm=True)
        await asyncio.wait_for(bench.handler.reached.wait(), timeout=60.0)
        assert bench.plate_site() == PLATE_SITE

        left_as_it_is = await bench.runtime.incidents.recoverable_timeout_mark_complete(
            incident.id, "operator", reason="the pick did finish", confirm=True,
        )

        assert bench.plate_site() == PLATE_SITE, "the stored pick was replayed over a plate that has moved on"
        assert left_as_it_is is True
        [settled] = await bench.outcome_unknown_incidents()
        assert settled.acknowledged is True


async def test_mark_complete_after_the_plate_came_back_to_where_the_pick_started_does_not_replay_the_pick() -> None:
    """The pick from the stacker fails, a person retries, and the run carries the plate to the deck and back to the
    stacker. The plate is where the old pick started, but it got there after the incident, so the pick is not replayed.
    """
    async with _bench(pick_fails=True, ends_at="stacker") as bench:
        bench.handler.finish.set()
        execution_id = await bench.run()
        thread = await bench.paused_thread(execution_id)
        [incident] = await bench.outcome_unknown_incidents()
        started_at = bench.plate_site()

        await bench.runtime.threads.recover(execution_id, thread.id, RecoveryDecision.RETRY, confirm=True)
        await run_to_quiescence(bench.runtime, execution_id, timeout=60.0)
        assert bench.plate_site() == started_at, "the run brought the plate back to where the pick started"

        left_as_it_is = await bench.runtime.incidents.recoverable_timeout_mark_complete(
            incident.id, "operator", reason="the pick did finish", confirm=True,
        )

        assert bench.plate_site() == started_at, "the stored pick was replayed and put the plate back in the jaws"
        assert left_as_it_is is True
