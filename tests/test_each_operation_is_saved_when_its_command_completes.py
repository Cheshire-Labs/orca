"""An operation is on the record the moment its command returns, so the next command in the same action sees it."""

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from orca.runtime.incident_store import IncidentCategory

import orca.orca as orca
from cheshire_drivers import (
    CartesianCoordinates as C,
    DeckLayoutConfig,
    DeckResourceConfig,
    RecordingLiquidHandlerDriver,
    Teachpoint,
)
from cheshire_drivers.liquid_handler_models import (
    AspirateRequest,
    DiscardTipsRequest,
    DispenseRequest,
    LabwareStateResponse,
    PickUpTipsRequest,
)
from cheshire_drivers.plr import ChatterboxLiquidHandlerWithProtocolDriver
from orca.devices.device_interfaces import ILiquidHandler
from orca.devices.devices import LiquidHandler, Storage
from orca.events.event_bus import EventBus
from orca.events.runtime_event import RuntimeEvent
from orca.resource_models.labware import LabwareInitialState, TipRackInstance
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.modes.run_modes import WorkflowRunMode
from orca.sdk.build import SystemBuild, Topology
from orca.sdk.labware import PlateTemplate, TipRackTemplate
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from orca.state.ops_store import IOpsHistoryStore
from orca.state.records import (
    DeviceOperation,
    GenericOperationDetails,
    ObservationGapCause,
    ObservationGapDetails,
    OperationRecord,
    TrackingRecord,
)
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.status_enums import RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from tests.test_deck_resident_reagent_pipetting import build_pipetting_bench
from orca.runtime.system_runtime import SystemRuntime
from tests.ephemeral_ops_store import EphemeralOpsHistoryStore
from tests.memory_activity import memory_ops_store, memory_runtime
from tests.setup_package import package_stores, with_deck_layouts, with_teachpoints, write_deck_layouts, write_teachpoints
from tests.test_helpers import run_to_quiescence, wait_for_paused_thread, wait_for_runtime_condition
from tests.real_device_path import the_real_device_path
from tests.recording_handler import RecordingHandler

DECK = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-15", catalog_ref="TIP_CAR_480_A00", rail=15),
    ],
)
TIPS_SITE = "lh/carrier-15-0"
ARM_DECK_ENTRY = "lh/carrier-7-2"

Body = Callable[[ActionContext], Awaitable[None]]


async def _build(
    recorder: RecordingLiquidHandlerDriver, body: Body, ops_store: IOpsHistoryStore | None = None,
) -> SystemBuild:
    stores = package_stores()
    plate = PlateTemplate(
        "plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
        initial_state=LabwareInitialState(uniform_volume=100.0),
    )
    tips = TipRackTemplate("tips", labware_type="hamilton_96_tiprack_10uL_filter", with_tips=True)
    the_real_device_path().simulate_with("lh", recorder)
    lh = with_deck_layouts(
        LiquidHandler("lh", deck_layout="default"), write_deck_layouts(stores, "lh", {"default": DECK}),
    )
    stacker = Storage("stacker")
    waste = Storage("waste")
    pad = PlatePad("pad")
    arm = with_teachpoints(Transporter("arm"), write_teachpoints(stores, "arm", [
        Teachpoint("stacker", C(0, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("pad", C(200, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint(ARM_DECK_ENTRY, C(400, 200, 300, 0, 90, 180), orientation="right"),
        Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
    ]))

    @orca.action(device=lh, inputs=[plate, tips], deck_positions={plate: "carrier-7-0", tips: "carrier-15-0"})
    async def work(ctx: ActionContext) -> None:
        await body(ctx)

    @orca.method
    async def work_method(ctx: MethodContext):
        yield work

    @orca.thread(labware=plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield work_method

    @orca.thread(labware=tips, start=(TIPS_SITE, REUSE_EXISTING), end=(TIPS_SITE, LEAVE_IN_PLACE))
    async def tips_journey(ctx: ThreadContext):
        yield orca.join(allows=[work_method])

    @orca.workflow(name="one_action")
    def workflow(wf):
        wf.start(plate_journey)
        wf.thread(tips_journey)

    topology = Topology(locations={"stacker": stacker, "pad": pad, "lh": lh, "waste": waste}, transporters=[arm])
    return await orca.build_system(name="One action", workflow=workflow, topology=topology, stores=stores, event_bus=EventBus(), ops_history_store=ops_store or memory_ops_store())


def _picks(recorder: RecordingLiquidHandlerDriver) -> list[list[str]]:
    return [
        [str(p) for pick in call.args["picks"] for p in pick["positions"]]
        for call in recorder.calls if call.method == "pick_up_tips"
    ]


async def _run(body: Body, *, until_paused: bool = False) -> tuple[RecordingLiquidHandlerDriver, str]:
    recorder = RecordingHandler()
    build = await _build(recorder, body)
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow("one_action", mode=WorkflowRunMode.CHECK_RUN)
        if until_paused:
            paused = await wait_for_paused_thread(runtime, record.id, timeout=60.0)
            return recorder, paused.last_error or paused.pause_message or ""
        await run_to_quiescence(runtime, record.id, timeout=90.0)
        return recorder, ""
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_each_pick_in_one_action_takes_the_next_tips() -> None:
    async def body(ctx: ActionContext) -> None:
        lh = ctx.device(ILiquidHandler)
        for _ in range(3):
            await lh.pick_up_tips(await ctx.next_tips("tips", 2))
            await lh.discard_tips()

    recorder, _ = await _run(body)

    assert _picks(recorder) == [["A1", "B1"], ["C1", "D1"], ["E1", "F1"]]


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_the_rack_is_down_a_tip_as_soon_as_the_pick_returns() -> None:
    counts: list[int] = []

    async def body(ctx: ActionContext) -> None:
        lh = ctx.device(ILiquidHandler)
        rack = ctx.labware("tips", TipRackInstance)
        counts.append(await rack.tip_count_present())
        await lh.pick_up_tips(await ctx.next_tips("tips", 2))
        counts.append(await rack.tip_count_present())
        await lh.discard_tips()

    await _run(body)

    assert counts == [96, 94]


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_picking_the_same_position_twice_in_one_action_is_refused() -> None:
    async def body(ctx: ActionContext) -> None:
        lh = ctx.device(ILiquidHandler)
        rack = ctx.tip_rack("tips")
        await lh.pick_up_tips([rack.tip_spot("A1")])
        await lh.discard_tips()
        await lh.pick_up_tips([rack.tip_spot("A1")])

    recorder, message = await _run(body, until_paused=True)

    assert _picks(recorder) == [["A1"]]
    assert "A1" in message


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_an_aspirate_is_on_the_plates_record_before_the_next_command() -> None:
    aspirates_seen: list[int] = []

    async def body(ctx: ActionContext) -> None:
        lh = ctx.device(ILiquidHandler)
        plate = ctx.labware("plate")

        async def aspirates() -> int:
            return sum(1 for op in await plate.ops() if op.operation is DeviceOperation.ASPIRATE)

        await lh.pick_up_tips(await ctx.next_tips("tips", 1))
        aspirates_seen.append(await aspirates())
        await lh.aspirate([ctx.plate("plate").well("A1")], [5.0])
        aspirates_seen.append(await aspirates())
        await lh.discard_tips()

    await _run(body)

    assert aspirates_seen == [0, 1]


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_tip_returned_to_the_rack_can_be_picked_again_in_the_same_action() -> None:
    async def body(ctx: ActionContext) -> None:
        lh = ctx.device(ILiquidHandler)
        rack = ctx.tip_rack("tips")
        await lh.pick_up_tips([rack.tip_spot("A1")])
        await lh.drop_tips([rack.tip_spot("A1")])
        await lh.pick_up_tips([rack.tip_spot("A1")])
        await lh.discard_tips()

    recorder, _ = await _run(body)

    assert _picks(recorder) == [["A1"], ["A1"]]


class _DispenseFails(RecordingHandler):
    async def dispense(self, request: DispenseRequest) -> LabwareStateResponse:
        raise RuntimeError("Simulated dispense failure")


async def _paused_at_a_failed_dispense(driver: _DispenseFails) -> tuple[SystemRuntime, str, str]:
    build, _ = await build_pipetting_bench(driver, reservoir_volume=1000.0)
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    await runtime.devices.set_mounted_tips("lh", {}, reason="the head is bare", confirm=True)
    record = await runtime.submit_workflow("pipetting_wf", mode=WorkflowRunMode.CHECK_RUN)
    paused = await wait_for_paused_thread(runtime, record.id, timeout=30.0)
    return runtime, record.id, paused.id


def _gaps(ops: list[OperationRecord]) -> list[ObservationGapCause]:
    return [op.details.cause for op in ops if isinstance(op.details, ObservationGapDetails)]


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_an_action_that_fails_has_already_saved_what_it_did() -> None:
    driver = _DispenseFails(ChatterboxLiquidHandlerWithProtocolDriver(num_channels=8))
    runtime, _, _ = await _paused_at_a_failed_dispense(driver)
    try:
        ids = {lw.template_name: lw.id for lw in await runtime.labware.list_all()}
        picked = [str(p) for call in driver.calls if call.method == "pick_up_tips" for pick in call.args["picks"] for p in pick["positions"]]

        rack = await runtime.labware.get_tip_state(ids["tips"])
        reservoir = await runtime.labware.get_well_volumes(ids["reservoir"])

        assert picked and not set(picked) & set(rack.positions_present)
        assert (await runtime.system.mounted_tips.of("lh")).by_channel
        assert sum(reservoir.volumes.values()) < 1000.0
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_an_abort_keeps_what_the_action_really_did() -> None:
    driver = _DispenseFails(ChatterboxLiquidHandlerWithProtocolDriver(num_channels=8))
    runtime, execution_id, thread_id = await _paused_at_a_failed_dispense(driver)
    try:
        runtime.recover_thread(execution_id, thread_id, RecoveryDecision.ABORT_THREAD)
        await run_to_quiescence(runtime, execution_id)

        assert (await runtime.system.mounted_tips.of("lh")).by_channel
        rack = next(lw for lw in runtime.system.labwares if lw.template_name == "tips")
        assert ObservationGapCause.OPERATIONS_DROPPED not in _gaps(await rack.ops())
    finally:
        await runtime.shutdown()


class _AspirateNeverReturns(RecordingHandler):
    def __init__(self, inner: ChatterboxLiquidHandlerWithProtocolDriver) -> None:
        super().__init__(inner)
        self.reached = asyncio.Event()

    async def aspirate(self, request: AspirateRequest) -> LabwareStateResponse:
        self.reached.set()
        await asyncio.Event().wait()
        raise AssertionError("this aspirate never returns")


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_stop_that_cuts_a_command_off_marks_the_record_unknown() -> None:
    driver = _AspirateNeverReturns(ChatterboxLiquidHandlerWithProtocolDriver(num_channels=8))
    build, _ = await build_pipetting_bench(driver, reservoir_volume=1000.0)
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow("pipetting_wf", mode=WorkflowRunMode.CHECK_RUN)
        await asyncio.wait_for(driver.reached.wait(), timeout=30.0)

        await asyncio.wait_for(runtime.stop_execution(record.id), timeout=5.0)
        await asyncio.wait_for(runtime.stop_execution(record.id, confirm=True), timeout=10.0)

        await runtime.system.command_pipeline.stop_reports_written()
        reservoir = next(lw for lw in runtime.system.labwares if lw.template_name == "reservoir")
        assert ObservationGapCause.OPERATIONS_DROPPED in _gaps(await reservoir.ops()), "the aspirate names the reservoir"
        rack = next(lw for lw in runtime.system.labwares if lw.template_name == "tips")
        assert ObservationGapCause.OPERATIONS_DROPPED not in _gaps(await rack.ops()), "the aspirate does not name the rack"
        stopped = await runtime.incidents.list(category=IncidentCategory.RECOVERABLE_TIMEOUT)
        assert [("aspirate" in incident.message, "stopped" in incident.message) for incident in stopped] == [
            (True, True),
        ], "a command stopped mid-way asks someone to confirm where the plate is"
    finally:
        await runtime.shutdown()


class _CountsSaves:
    def __init__(self) -> None:
        self.saves = 0

    def on_event(self, event: RuntimeEvent) -> None:
        if event.entity_type == "CONTENTS":
            self.saves += 1


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_save_is_announced_before_the_next_command_can_run() -> None:
    """A display refreshing on the command's own finish can read before the save; this event cannot."""
    announced = _CountsSaves()
    seen: list[int] = []

    async def body(ctx: ActionContext) -> None:
        lh = ctx.device(ILiquidHandler)
        seen.append(announced.saves)
        await lh.pick_up_tips(await ctx.next_tips("tips", 2))
        seen.append(announced.saves)
        await lh.discard_tips()

    recorder = RecordingHandler()
    build = await _build(recorder, body)
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    runtime.register_sink(announced)
    await runtime.start()
    try:
        record = await runtime.submit_workflow("one_action", mode=WorkflowRunMode.CHECK_RUN)
        await run_to_quiescence(runtime, record.id, timeout=90.0)
    finally:
        await runtime.shutdown()

    assert seen[1] > seen[0]


async def _tips_left(runtime: SystemRuntime) -> int:
    rack = next(lw for lw in runtime.system.labwares if lw.template_name == "tips")
    assert isinstance(rack, TipRackInstance)
    return await rack.tip_count_present()


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_standalone_method_saves_each_pick_as_a_workflow_does() -> None:
    async def body(ctx: ActionContext) -> None:
        lh = ctx.device(ILiquidHandler)
        for _ in range(2):
            await lh.pick_up_tips(await ctx.next_tips("tips", 2))
            await lh.discard_tips()

    recorder = RecordingHandler()
    build = await _build(recorder, body)
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_method(
            "one_action", "work_method",
            labware_start={"plate": "pad", "tips": TIPS_SITE},
            labware_end={"plate": "pad", "tips": TIPS_SITE},
            mode=WorkflowRunMode.CHECK_RUN,
        )
        await run_to_quiescence(runtime, record.id, timeout=90.0)
        assert await _tips_left(runtime) == 92
    finally:
        await runtime.shutdown()

    assert _picks(recorder) == [["A1", "B1"], ["C1", "D1"]]


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_an_action_retry_counts_each_real_pick_once() -> None:
    passes: list[int] = []

    async def body(ctx: ActionContext) -> None:
        lh = ctx.device(ILiquidHandler)
        await lh.pick_up_tips(await ctx.next_tips("tips", 2))
        await lh.discard_tips()
        passes.append(1)
        if len(passes) == 1:
            raise ZeroDivisionError("the action's own code failed after the discard")

    driver = RecordingHandler()
    build = await _build(driver, body)
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow("one_action", mode=WorkflowRunMode.CHECK_RUN)
        paused = await wait_for_paused_thread(runtime, record.id, timeout=60.0)
        assert await _tips_left(runtime) == 94

        runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY_ACTION)
        await run_to_quiescence(runtime, record.id, timeout=60.0)

        assert await _tips_left(runtime) == 92
    finally:
        await runtime.shutdown()

    assert _picks(driver) == [["A1", "B1"], ["C1", "D1"]]


class _DiscardFailsOnceAfterDropping(RecordingHandler):
    def __init__(self, inner: ChatterboxLiquidHandlerWithProtocolDriver) -> None:
        super().__init__(inner)
        self.failed = False

    async def discard_tips(self, request: DiscardTipsRequest) -> LabwareStateResponse:
        response = await super().discard_tips(request)
        if not self.failed:
            self.failed = True
            raise RuntimeError("Simulated failure after the tips were dropped")
        return response


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_step_retry_of_a_discard_that_dropped_the_tips_repeats_no_pick() -> None:
    """RETRY re-sends only the discard. The tips are already gone, so it fails
    again and the thread pauses again; CONTINUE, which says the call really
    happened, finishes the run with the one real pick on the record."""
    async def body(ctx: ActionContext) -> None:
        lh = ctx.device(ILiquidHandler)
        await lh.pick_up_tips(await ctx.next_tips("tips", 2))
        await lh.discard_tips()

    driver = _DiscardFailsOnceAfterDropping(ChatterboxLiquidHandlerWithProtocolDriver(num_channels=8))
    build = await _build(driver, body)
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow("one_action", mode=WorkflowRunMode.CHECK_RUN)
        paused = await wait_for_paused_thread(runtime, record.id, timeout=60.0)
        assert paused.pause_site == "DEVICE_OP"
        assert await _tips_left(runtime) == 94

        await the_real_device_path().gateway.controller.clear_fault("lh")
        runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY)
        await wait_for_runtime_condition(
            runtime,
            lambda: any(
                "No tips have been picked up" in (t.last_error or "")
                for t in runtime.get_paused_threads(record.id)
            ),
            timeout=60.0,
            message="the re-sent discard did not pause the thread again",
        )
        again = runtime.get_paused_threads(record.id)[0]
        assert again.pause_site == "DEVICE_OP"
        assert _picks(driver) == [["A1", "B1"]]
        assert await _tips_left(runtime) == 94

        await the_real_device_path().gateway.controller.clear_fault("lh")
        runtime.recover_thread(record.id, again.id, RecoveryDecision.CONTINUE)
        statuses = await run_to_quiescence(runtime, record.id, timeout=60.0)

        assert set(statuses.values()) == {"COMPLETED"}
        assert await _tips_left(runtime) == 94
    finally:
        await runtime.shutdown()

    assert _picks(driver) == [["A1", "B1"]]


class _PickFailsOnce(RecordingHandler):
    def __init__(self, inner: ChatterboxLiquidHandlerWithProtocolDriver) -> None:
        super().__init__(inner)
        self.failed = False

    async def pick_up_tips(self, request: PickUpTipsRequest) -> LabwareStateResponse:
        if not self.failed:
            self.failed = True
            raise RuntimeError("Simulated pickup failure")
        return await super().pick_up_tips(request)


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_failed_pick_saves_nothing_and_its_retry_saves_once() -> None:
    async def body(ctx: ActionContext) -> None:
        lh = ctx.device(ILiquidHandler)
        await lh.pick_up_tips(await ctx.next_tips("tips", 2))
        await lh.discard_tips()

    driver = _PickFailsOnce(ChatterboxLiquidHandlerWithProtocolDriver(num_channels=8))
    build = await _build(driver, body)
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow("one_action", mode=WorkflowRunMode.CHECK_RUN)
        paused = await wait_for_paused_thread(runtime, record.id, timeout=60.0)
        assert await _tips_left(runtime) == 96

        await the_real_device_path().gateway.controller.clear_fault("lh")
        runtime.recover_thread(record.id, paused.id, RecoveryDecision.RETRY)
        await run_to_quiescence(runtime, record.id, timeout=60.0)

        assert await _tips_left(runtime) == 94
    finally:
        await runtime.shutdown()


async def _nothing_to_do(ctx: ActionContext) -> None:
    return None


def _arm_commands(records: list[TrackingRecord]) -> list[str]:
    return [
        op.details.command for record in records for op in record.operations
        if op.device_name == "arm" and isinstance(op.details, GenericOperationDetails)
    ]


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_runs_picks_and_places_are_saved_in_its_history() -> None:
    store = memory_ops_store()
    build = await _build(RecordingHandler(), _nothing_to_do, store)
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow("one_action", mode=WorkflowRunMode.CHECK_RUN)
        await run_to_quiescence(runtime, record.id, timeout=90.0)
        saved = _arm_commands(await store.list(record.id))
    finally:
        await runtime.shutdown()

    # stacker -> lh -> waste: two moves, each a pick and a place.
    assert [c for c in saved if c in ("pick_at_coords", "place_at_coords")] == [
        "pick_at_coords", "place_at_coords", "pick_at_coords", "place_at_coords",
    ]


class _ArmCommandIsNotSaved(EphemeralOpsHistoryStore):
    def __init__(self, command: str) -> None:
        super().__init__()
        self._command = command

    async def append(self, execution_id: str, record: TrackingRecord) -> None:
        if self._command in _arm_commands([record]):
            raise OSError("disk full")
        await super().append(execution_id, record)


async def _paused_where_the_plate_is(command: str) -> tuple[str, str]:
    """Run until the arm's `command` fails to save; the pause message, and where the record has the plate."""
    build = await _build(
        RecordingHandler(), _nothing_to_do,
        _ArmCommandIsNotSaved(command),
    )
    runtime = await memory_runtime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow("one_action", mode=WorkflowRunMode.CHECK_RUN)
        paused = await wait_for_paused_thread(runtime, record.id, timeout=60.0)
        plate = next(lw for lw in runtime.system.labwares if lw.template_name == "plate")
        where = runtime.system.labware_location_service.get(plate).position_id
        return paused.last_error or "", where
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_pick_whose_save_fails_fails_the_move() -> None:
    message, _ = await _paused_where_the_plate_is("pick_at_coords")

    assert "'pick_at_coords' ran on 'arm'" in message
    assert "do not send it again" in message


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_pick_whose_save_fails_leaves_the_plate_recorded_in_the_jaws() -> None:
    _, where = await _paused_where_the_plate_is("pick_at_coords")

    assert where == "arm/gripper"


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_place_whose_save_fails_leaves_the_plate_recorded_at_the_target() -> None:
    _, where = await _paused_where_the_plate_is("place_at_coords")

    assert where == ARM_DECK_ENTRY
