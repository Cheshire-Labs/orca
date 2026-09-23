"""Several labware co-locate on a liquid handler in a single action.

A reagent-heavy action lists four labware on the LH at once: a transit working
plate (the owner, routed to its own deck site via deck_positions) plus three
deck-resident labware on distinct sites: two reagent troughs and a tip rack.
They must all be present and addressable during the one action. If they could
not co-locate (e.g. the residents collided on one site or the transit plate
never left the arm's deck entry site), the action could never gather them and the
workflow would hang at reservation collection, so reaching COMPLETED with
commands issued against the distinct reagents is the proof of real co-location.
"""

import asyncio

import pytest

import orca.orca as orca
from orca.runtime.execution import ExecutionPhase
from orca.runtime.submission import SubmissionStatus
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from cheshire_drivers import (
    CartesianCoordinates as C,
    DeckLayoutConfig,
    DeckResourceConfig,
    RecordingLiquidHandlerDriver,
    Teachpoint,
)
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from cheshire_drivers.liquid_handler_models import GetDeckStateRequest
from orca.devices.device_interfaces import ILiquidHandler
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate, TipRackTemplate
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.test_helpers import RecordingLhDeckFactory, run_to_quiescence, wait_for_runtime_condition, named_for_template, template_of


DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-25", catalog_ref="Trough_CAR_4R200_A00", rail=25),
        DeckResourceConfig(name="carrier-15", catalog_ref="TIP_CAR_480_A00", rail=15),
    ],
)

REAGENT_A_SITE = "lh/carrier-25-0"
REAGENT_B_SITE = "lh/carrier-25-1"
TIPS_SITE = "lh/carrier-15-0"
# The arm reaches exactly this deck site; the gripper relays onward from it.
ARM_DECK_ENTRY = "lh/carrier-7-2"


async def _build_colocation(recorder: RecordingLiquidHandlerDriver):
    stores = InMemoryRuntimeStoreFactory()

    working_plate = PlateTemplate(
        "working_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    reagent_a = PlateTemplate(
        "reagent_a", labware_type="AGenBio_1_troughplate_190000uL_Fl")
    reagent_b = PlateTemplate(
        "reagent_b", labware_type="AGenBio_1_troughplate_190000uL_Fl")
    tips = TipRackTemplate(
        "tips", labware_type="hamilton_96_tiprack_10uL_filter", with_tips=True)

    with use_device_factory(RecordingLhDeckFactory(recorder)):
        lh = LiquidHandler(
            "lh",
            deck_layout_store=stores.deck_layouts("lh", seed={"default": DECK_CONFIG}),
            deck_layout="default",
        )
        stacker = Storage("stacker")
        waste = Storage("waste")
        pad = PlatePad("pad")
        arm = Transporter(
            "arm",
            teachpoint_store=stores.teachpoints("arm", seed=[
                Teachpoint("stacker", C(0, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("pad", C(200, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint(ARM_DECK_ENTRY, C(400, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
            ]),
        )

    @orca.action(
        device=lh,
        inputs=[working_plate, reagent_a, reagent_b, tips],
        deck_positions={working_plate: "carrier-7-0"},
    )
    async def combine_reagents(ctx: ActionContext) -> None:
        handler = ctx.device(ILiquidHandler)
        rack = ctx.tip_rack("tips")
        a = ctx.plate("reagent_a").well("A1")
        b = ctx.plate("reagent_b").well("A1")
        await handler.pick_up_tips([rack.tip_spot("A1")])
        await handler.aspirate([a], [10.0])
        await handler.aspirate([b], [10.0])
        await handler.dispense([a], [20.0])
        await handler.drop_tips([rack.tip_spot("A1")])

    @orca.method
    async def combine_method(ctx: MethodContext):
        yield combine_reagents

    @orca.thread(labware=working_plate, start=("stacker", DISPENSE), end="waste")
    async def plate_journey(ctx: ThreadContext):
        yield combine_method

    @orca.thread(
        labware=reagent_a,
        start=(REAGENT_A_SITE, REUSE_EXISTING),
        end=(REAGENT_A_SITE, LEAVE_IN_PLACE),
    )
    async def reagent_a_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[combine_method])

    @orca.thread(
        labware=reagent_b,
        start=(REAGENT_B_SITE, REUSE_EXISTING),
        end=(REAGENT_B_SITE, LEAVE_IN_PLACE),
    )
    async def reagent_b_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[combine_method])

    @orca.thread(
        labware=tips,
        start=(TIPS_SITE, REUSE_EXISTING),
        end=(TIPS_SITE, LEAVE_IN_PLACE),
    )
    async def tips_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[combine_method])

    @orca.workflow(name="colocation_wf")
    def workflow(wf):
        wf.start(plate_journey)
        wf.thread(reagent_a_journey)
        wf.thread(reagent_b_journey)
        wf.thread(tips_journey)

    topology = Topology(
        locations={"stacker": stacker, "pad": pad, "lh": lh, "waste": waste},
        transporters=[arm],
    )
    build = await orca.build_system(
        name="Co-location", workflow=workflow, topology=topology, stores=stores)
    return build, lh


def _aspirated_labware(recorder: RecordingLiquidHandlerDriver) -> set[str]:
    names: set[str] = set()
    for call in recorder.calls:
        if call.method != "aspirate":
            continue
        for target in call.args["aspirations"]:
            names.add(str(target["labware"]))
    return names


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_multiple_reagents_colocate_in_one_action() -> None:
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, lh = await _build_colocation(recorder)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    record = await runtime.submit_workflow("colocation_wf", mode=WorkflowRunMode.PURE_SIM)
    statuses = await run_to_quiescence(runtime, record.id)
    try:
        assert statuses, "no threads were created"
        assert all(s == "COMPLETED" for s in statuses.values()), (
            f"every thread must reach COMPLETED; a hang here means the four "
            f"labware could not co-locate and collided on the arm's entry site: "
            f"{statuses}")

        assert {template_of(n) for n in _aspirated_labware(recorder)} == {"reagent_a", "reagent_b"}, (
            f"the action must aspirate from both distinct co-located reagents, "
            f"got {_aspirated_labware(recorder)}")

        deck = await lh.driver.get_deck_state(GetDeckStateRequest())
        deck_names = {item.name for item in deck.labware}
        assert all(any(named_for_template(n, t) for n in deck_names) for t in ("reagent_a", "reagent_b", "tips")), (
            f"the three resident labware must be on the driver deck "
            f"simultaneously; deck held {deck_names}")
    finally:
        await runtime.shutdown()


async def _build_two_transit_no_sites(
    recorder: RecordingLiquidHandlerDriver, ran: list[bool],
):
    """Two transit plates co-located on the LH in one action, with NO deck_positions.

    The owner (plate_a) is an entry thread; the joiner (plate_b) auto-spawns.
    Neither declares a deck site, so neither has a working site to rest on.
    `ran` records whether the action body executed.
    """
    stores = InMemoryRuntimeStoreFactory()
    plate_a = PlateTemplate(
        "plate_a", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    plate_b = PlateTemplate(
        "plate_b", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")

    with use_device_factory(RecordingLhDeckFactory(recorder)):
        lh = LiquidHandler(
            "lh",
            deck_layout_store=stores.deck_layouts("lh", seed={"default": DECK_CONFIG}),
            deck_layout="default",
        )
        stacker_a = Storage("stacker_a")
        stacker_b = Storage("stacker_b")
        waste = Storage("waste")
        arm = Transporter(
            "arm",
            teachpoint_store=stores.teachpoints("arm", seed=[
                Teachpoint("stacker_a", C(0, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("stacker_b", C(100, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint(ARM_DECK_ENTRY, C(400, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
            ]),
        )

    @orca.action(device=lh, inputs=[plate_a, plate_b])
    async def combine(ctx: ActionContext) -> None:
        ran.append(True)

    @orca.method
    async def combine_method(ctx: MethodContext):
        yield combine

    @orca.thread(labware=plate_a, start=("stacker_a", DISPENSE), end="waste")
    async def owner_journey(ctx: ThreadContext):
        yield combine_method

    @orca.thread(labware=plate_b, start=("stacker_b", DISPENSE), end="waste")
    async def joiner_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[combine_method])

    @orca.workflow(name="transit_no_site_wf")
    def workflow(wf):
        wf.start(owner_journey)
        wf.thread(joiner_journey)

    topology = Topology(
        locations={"stacker_a": stacker_a, "stacker_b": stacker_b,
                   "lh": lh, "waste": waste},
        transporters=[arm],
    )
    build = await orca.build_system(
        name="TransitNoSite", workflow=workflow, topology=topology, stores=stores)
    return build


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_transit_input_without_deck_site_fails_fast() -> None:
    """A transit input to a multi-site LH with no deck_positions must fail the
    execution with a clear error, not silently complete (sim) or hang (live)."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    ran: list[bool] = []
    build = await _build_two_transit_no_sites(recorder, ran)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            "transit_no_site_wf", mode=WorkflowRunMode.PURE_SIM)
        execution = runtime._executions[record.id]
        try:
            await asyncio.wait_for(asyncio.shield(execution.task), timeout=60)
        except BaseException:
            pass
        try:
            await wait_for_runtime_condition(
                runtime,
                lambda: execution.phase in (
                    ExecutionPhase.FAILED, ExecutionPhase.ABORTED, ExecutionPhase.COMPLETED,
                ),
                timeout=4.0,
            )
        except TimeoutError:
            pass

        assert execution.phase is ExecutionPhase.FAILED, (
            f"a transit input with no deck_positions must fail the execution; "
            f"got phase={execution.phase} error={execution.error!r}")
        assert "deck" in (execution.error or "").lower(), (
            f"the failure must name the missing deck site; got {execution.error!r}")
        assert not ran, (
            "the action body must not run when a transit input lacks a deck site")
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_spawned_contributor_missing_deck_site_fails_execution(monkeypatch) -> None:
    """A transit CONTRIBUTOR auto-spawned via orca.join, lacking a deck site, must
    stop its owner promptly -- not leave the sited owner waiting out
    CO_LABWARE_TIMEOUT for a plate that will never arrive.

    The owner is parked for an operator decision rather than the run failing
    itself: a crashed thread no longer tears the execution down. What the
    operator has to be given is why, so the missing deck site has to be on the
    incidents surface, not only in a log line."""
    from orca.workflow_models.labware_threads.executing_labware_thread import (
        ExecutingLabwareThread,
    )
    # Safety net: if escalation fails to fire, the owner times out in seconds
    # (a timeout, not "deck") instead of hanging on the now-unbounded wait.
    monkeypatch.setattr(
        ExecutingLabwareThread, "co_labware_timeout",
        property(lambda self: 5.0),
    )
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    ran: list[bool] = []
    stores = InMemoryRuntimeStoreFactory()
    plate_a = PlateTemplate(
        "plate_a", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    plate_b = PlateTemplate(
        "plate_b", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")

    with use_device_factory(RecordingLhDeckFactory(recorder)):
        lh = LiquidHandler(
            "lh",
            deck_layout_store=stores.deck_layouts("lh", seed={"default": DECK_CONFIG}),
            deck_layout="default",
        )
        stacker_a = Storage("stacker_a")
        stacker_b = Storage("stacker_b")
        waste = Storage("waste")
        arm = Transporter(
            "arm",
            teachpoint_store=stores.teachpoints("arm", seed=[
                Teachpoint("stacker_a", C(0, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("stacker_b", C(100, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint(ARM_DECK_ENTRY, C(400, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
            ]),
        )

    # Owner (plate_a) IS sited; the auto-spawned contributor (plate_b) is not.
    @orca.action(
        device=lh, inputs=[plate_a, plate_b],
        deck_positions={plate_a: "carrier-7-0"},
    )
    async def combine(ctx: ActionContext) -> None:
        ran.append(True)

    @orca.method
    async def combine_method(ctx: MethodContext):
        yield combine

    @orca.thread(labware=plate_a, start=("stacker_a", DISPENSE), end="waste")
    async def owner_journey(ctx: ThreadContext):
        yield combine_method

    @orca.thread(labware=plate_b, start=("stacker_b", DISPENSE), end="waste")
    async def joiner_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[combine_method])

    @orca.workflow(name="spawned_contributor_no_site_wf")
    def workflow(wf):
        wf.start(owner_journey)
        wf.thread(joiner_journey)

    topology = Topology(
        locations={"stacker_a": stacker_a, "stacker_b": stacker_b,
                   "lh": lh, "waste": waste},
        transporters=[arm],
    )
    build = await orca.build_system(
        name="SpawnedNoSite", workflow=workflow, topology=topology, stores=stores)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            "spawned_contributor_no_site_wf", mode=WorkflowRunMode.PURE_SIM)
        execution = runtime._executions[record.id]
        try:
            await asyncio.wait_for(asyncio.shield(execution.task), timeout=30)
        except BaseException:
            pass
        await wait_for_runtime_condition(
            runtime,
            lambda: any(
                t.name.startswith("plate_a") and t.status == "PAUSED"
                for t in runtime.list_threads(record.id)
            ),
            timeout=8.0,
            message=(
                "the owner must be stopped by its contributor's death, well "
                "inside the co-labware wait it would otherwise sit out"
            ),
        )

        incidents = await runtime.incidents.list()
        deck_named = [
            inc for inc in incidents
            if "deck_positions" in inc.message or "deck site" in inc.message
        ]
        assert deck_named, (
            "the operator has to be told the deck site is missing, not left "
            f"reading a co-labware timeout; incidents={[i.message for i in incidents]}")
        assert not ran, "the action body must not run"
    finally:
        await runtime.shutdown()


async def _build_owner_unsited_with_resident(
    recorder: RecordingLiquidHandlerDriver, ran: list[bool],
):
    """Entry OWNER is transit and unsited; its contributor is a deck-RESIDENT.

    The owner (plate_a, an entry thread) auto-spawns the resident reservoir when
    its action needs the reagent; the reservoir binds to its deck site and waits
    at co-labware. The owner then hits the guard and fails. The reservoir is a
    resident (guard-exempt), so it never fails on its own -- only the entry owner
    does. This isolates the entry-thread teardown: the orphaned resident must be
    cancelled, not left waiting out CO_LABWARE_TIMEOUT.
    """
    stores = InMemoryRuntimeStoreFactory()
    plate_a = PlateTemplate(
        "plate_a", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    reservoir = PlateTemplate(
        "reservoir", labware_type="AGenBio_1_troughplate_190000uL_Fl")

    with use_device_factory(RecordingLhDeckFactory(recorder)):
        lh = LiquidHandler(
            "lh",
            deck_layout_store=stores.deck_layouts("lh", seed={"default": DECK_CONFIG}),
            deck_layout="default",
        )
        stacker_a = Storage("stacker_a")
        waste = Storage("waste")
        arm = Transporter(
            "arm",
            teachpoint_store=stores.teachpoints("arm", seed=[
                Teachpoint("stacker_a", C(0, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint(ARM_DECK_ENTRY, C(400, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("waste", C(600, 200, 300, 0, 90, 180), orientation="right"),
            ]),
        )

    # No deck_positions: plate_a (transit) is unsited; reservoir is a resident.
    @orca.action(device=lh, inputs=[plate_a, reservoir])
    async def combine(ctx: ActionContext) -> None:
        ran.append(True)

    @orca.method
    async def combine_method(ctx: MethodContext):
        yield combine

    @orca.thread(labware=plate_a, start=("stacker_a", DISPENSE), end="waste")
    async def owner_journey(ctx: ThreadContext):
        yield combine_method

    @orca.thread(
        labware=reservoir,
        start=("lh/carrier-25-0", REUSE_EXISTING),
        end=("lh/carrier-25-0", LEAVE_IN_PLACE),
    )
    async def reservoir_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[combine_method])

    @orca.workflow(name="owner_unsited_resident_wf")
    def workflow(wf):
        wf.start(owner_journey)
        wf.thread(reservoir_journey)

    topology = Topology(
        locations={"stacker_a": stacker_a, "lh": lh, "waste": waste},
        transporters=[arm],
    )
    build = await orca.build_system(
        name="OwnerUnsitedResident", workflow=workflow, topology=topology, stores=stores)
    return build


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_entry_owner_unsited_fails_execution_loudly() -> None:
    """An unsited entry owner that fails the guard must fail the EXECUTION loudly
    and fast, not hang in ACCEPTING waiting out the co-labware timeout.

    The co-input is a deck RESIDENT (guard-exempt), so only the entry owner fails.
    This isolates the entry-thread failure path: the execution reports FAILED
    synchronously (via the same escalation the spawned-contributor path uses),
    within seconds of the guard raising -- far under the 300s default co-labware
    timeout. The regression it guards against hung in ACCEPTING until that timeout
    because the failure path inline-awaited a teardown that a co-labware-parked
    resident blocked. Cooperatively stopping the resident so it does not linger to
    the timeout is tracked separately; the finally here bounds test cleanup."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    ran: list[bool] = []
    build = await _build_owner_unsited_with_resident(recorder, ran)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    execution = None
    try:
        record = await runtime.submit_workflow(
            "owner_unsited_resident_wf", mode=WorkflowRunMode.PURE_SIM)
        execution = runtime._executions[record.id]

        # FAILED must land fast via synchronous escalation, not by waiting on any
        # co-labware timeout (now unbounded); 20s is ~10x the ~2s escalation needs.
        try:
            await wait_for_runtime_condition(
                runtime,
                lambda: execution.phase is ExecutionPhase.FAILED,
                timeout=20.0,
            )
        except TimeoutError:
            pass

        assert execution.phase is ExecutionPhase.FAILED, (
            f"the unsited entry owner must fail the execution fast, not hang in "
            f"ACCEPTING; got phase={execution.phase} error={execution.error!r}")
        assert "deck" in (execution.error or "").lower(), (
            f"the failure must name the missing deck site; got {execution.error!r}")
        assert not ran, "the action body must not run"
    finally:
        # Cooperatively stop the resident left parked at the co-labware wait (raw
        # cancel is not honored there) so cleanup is fast, not a 300s linger.
        if execution is not None and execution.executing_workflow is not None:
            for thread in execution.executing_workflow.threads:
                thread.stop()
            await run_to_quiescence(runtime, record.id)
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_escalation_failure_surfaces_on_every_terminal_channel() -> None:
    """A guard failure via the escalation path must surface on EVERY terminal
    channel, not just execution.phase. Otherwise the execution record stays
    RUNNING, submissions stay IN_PROGRESS, and no WS/record consumer learns the
    run failed. Assert the submissions go FAILED and both SUBMISSION.*.FAILED and
    EXECUTION.*.FAILED fire on a real failing run (the escalation path pre-sets
    FAILED, so _on_task_done skips its own emission -- the failure path must emit)."""
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    ran: list[bool] = []
    build = await _build_two_transit_no_sites(recorder, ran)
    captured: list[str] = []
    build.event_bus.subscribe_all(lambda name, ctx: captured.append(name))
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            "transit_no_site_wf", mode=WorkflowRunMode.PURE_SIM)
        execution = runtime._executions[record.id]
        try:
            await wait_for_runtime_condition(
                runtime,
                lambda: execution.phase is ExecutionPhase.FAILED,
                timeout=20.0,
            )
        except TimeoutError:
            pass

        assert execution.phase is ExecutionPhase.FAILED, (
            f"escalation must fail the execution; got phase={execution.phase}")
        assert execution.submissions, "the execution has no submissions to mark"
        assert all(s.status is SubmissionStatus.FAILED for s in execution.submissions), (
            f"submissions must be marked FAILED, not left IN_PROGRESS; got "
            f"{[s.status for s in execution.submissions]}")
        assert any(
            n.startswith("EXECUTION.") and n.endswith(".FAILED") for n in captured), (
            f"EXECUTION.*.FAILED must fire so the record/consumers learn of the "
            f"failure; captured terminal events: "
            f"{[n for n in captured if n.endswith(('.FAILED', '.COMPLETED', '.ABORTED'))]}")
        assert any(
            n.startswith("SUBMISSION.") and n.endswith(".FAILED") for n in captured), (
            f"SUBMISSION.*.FAILED must fire; captured: "
            f"{[n for n in captured if n.startswith('SUBMISSION.')]}")
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_tip_ops_fold_into_the_rack_instance_ledger() -> None:
    """Driver tip ops must join the rack INSTANCE's ledger: ledger_projections
    requires details.tip_rack == the instance name, which is now the PLR
    object's name. Dead before per-instance identity: the recorded name was the
    TEMPLATE's, no pick or drop ever folded, and tip depletion never worked
    outside hand-wired unit tests."""
    from orca.state.projections import tips_present
    from orca.state.records import TipPickUpDetails

    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, _ = await _build_colocation(recorder)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow("colocation_wf", mode=WorkflowRunMode.PURE_SIM)
        statuses = await run_to_quiescence(runtime, record.id)
        assert statuses and all(s == "COMPLETED" for s in statuses.values()), statuses
        tips_inst = next(lw for lw in build.system.labwares if lw.template_name == "tips")
        ops = await tips_inst.ops()
        picks = [op for op in ops if isinstance(op.details, TipPickUpDetails)]
        assert picks, "no TIP_PICK_UP op reached the rack instance's ledger"
        assert all(op.details.tip_rack == tips_inst.name for op in picks), (
            f"tip ops recorded under {[getattr(op.details, 'tip_rack', None) for op in picks]}, "
            f"not the instance name {tips_inst.name!r}; ledger projections "
            f"require the instance name, so these ops never fold"
        )
        # A1 was picked and returned in the action body; the fold must restore it.
        assert "A1" in tips_present(ops, tips_inst.name), (
            "pick/drop pair did not fold through tips_present"
        )
    finally:
        await runtime.shutdown()
