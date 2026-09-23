"""A rack the ledger says is spent must not be bound as a slot's first receiver.

Found on the bench 2026-09-04 running `run_7_batching_and_resupply` on a real
Flex. The rack on B2 was recorded with zero tips. The first contribution's
`transfer` asked for a column and the run error-paused inside the action body
with `NotEnoughTips`. No fresh rack was requested.

The capacity gate that would have caught it only ran when the slot ALREADY had
an active thread. On the first contribution there is none, so the engine bound
whatever rack was on the deck without asking whether it could supply a tip. The
same physical situation got two different behaviours, decided by whether the
slot happened to have been bound yet.

Running out of a consumable is a normal operating condition, not a fault. The
first ask reads the same as the fifth.

Unknown is not empty, and the two keep separate answers. The gate asks
`can_continue`, which answers True for a rack nothing has ever described, so
such a rack still binds and the operator settles it; only a rack the ledger
positively reports as short is refused. That carve-out is pinned by
`tests/unit/test_labware_can_continue.py::test_rack_with_no_seed_in_reach_reports_true`.
"""

import pytest

import orca.orca as orca
from cheshire_drivers import (
    CartesianCoordinates as C,
    DeckLayoutConfig,
    DeckResourceConfig,
    RecordingLiquidHandlerDriver,
    Teachpoint,
)
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from orca.devices.device_interfaces import ILiquidHandler
from orca.devices.devices import LiquidHandler, Storage
from orca.resource_models.capacity import CapacityPolicy, OverflowAction
from orca.resource_models.labware import LabwareInitialState
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate, TipRackTemplate
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.test_helpers import (
    RecordingLhDeckFactory,
    run_to_quiescence,
    wait_for_paused_thread,
)

DECK_CONFIG = DeckLayoutConfig(
    deck_type="STARlet",
    resources=[
        DeckResourceConfig(name="carrier-7", catalog_ref="PLT_CAR_L5AC_A00", rail=7),
        DeckResourceConfig(name="carrier-15", catalog_ref="TIP_CAR_480_A00", rail=15),
    ],
)

TIPS_SITE = "lh/carrier-15-0"
ARM_DECK_ENTRY = "lh/carrier-7-2"
_TIPS_PER_CONTRIBUTION = 2
# Exactly one contribution's worth, so a second contribution finds the rack dry.
_ONE_CONTRIBUTION = ["A1", "B1"]


async def _build(
    recorder: RecordingLiquidHandlerDriver,
    *,
    tip_positions: list[str] | None,
    with_tips: bool,
    contributions: int,
    tips_from_stacker: bool = False,
):
    """A plate that contributes `contributions` times into a deck-resident rack.

    ``tip_positions=None`` with ``with_tips=False`` is the rack the ledger
    positively calls empty. ``with_tips=True`` and no positions is a full rack.
    """
    stores = InMemoryRuntimeStoreFactory()

    working_plate = PlateTemplate(
        "working_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
    tips = TipRackTemplate(
        "tips",
        labware_type="hamilton_96_tiprack_10uL_filter",
        with_tips=with_tips,
        initial_state=(
            LabwareInitialState(tip_positions=list(tip_positions))
            if tip_positions is not None else None
        ),
    )

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

    deck_positions: dict[object, str] = {working_plate: "carrier-7-0"}
    if tips_from_stacker:
        # A transit rack needs its own named site; a resident one already has one.
        deck_positions[tips] = "carrier-15-0"

    @orca.action(device=lh, inputs=[working_plate, tips], deck_positions=deck_positions)
    async def transfer(ctx: ActionContext) -> None:
        handler = ctx.device(ILiquidHandler)
        await handler.pick_up_tips(await ctx.next_tips("tips", _TIPS_PER_CONTRIBUTION))
        await handler.discard_tips()

    @orca.method
    async def transfer_method(ctx: MethodContext):
        yield transfer

    @orca.thread(
        labware=working_plate,
        start=("stacker", DISPENSE),
        end="waste",
        contributes_to=["tips"],
    )
    async def plate_journey(ctx: ThreadContext):
        for _ in range(contributions):
            yield transfer_method

    if tips_from_stacker:
        @orca.thread(labware=tips, start=("stacker", DISPENSE), end="waste")
        async def tips_journey(ctx: ThreadContext):
            while ctx.has_more_work():
                yield orca.join(allows=[transfer_method])
    else:
        @orca.thread(
            labware=tips,
            start=(TIPS_SITE, REUSE_EXISTING),
            end=(TIPS_SITE, LEAVE_IN_PLACE),
        )
        async def tips_journey(ctx: ThreadContext):
            while ctx.has_more_work():
                yield orca.join(allows=[transfer_method])

    @orca.workflow(name="rack_supply")
    def workflow(wf):
        wf.start(plate_journey)
        wf.thread(
            tips_journey,
            capacity=CapacityPolicy(
                max_contributions=12, overflow_action=OverflowAction.NEW,
            ),
        )

    topology = Topology(
        locations={"stacker": stacker, "pad": pad, "lh": lh, "waste": waste},
        transporters=[arm],
    )
    return await orca.build_system(
        name="Rack supply", workflow=workflow, topology=topology, stores=stores)


def _pause_messages(runtime: SystemRuntime, execution_id: str) -> str:
    return " | ".join(
        f"{t.name}: {t.pause_message or t.last_error or '(no message)'}"
        for t in runtime.get_paused_threads(execution_id)
    )


def _picked_positions(recorder: RecordingLiquidHandlerDriver) -> list[list[str]]:
    picks: list[list[str]] = []
    for call in recorder.calls:
        if call.method != "pick_up_tips":
            continue
        positions: list[str] = []
        for pick in call.args["picks"]:
            positions.extend(str(p) for p in pick["positions"])
        picks.append(positions)
    return picks


async def _run(**kwargs) -> tuple[dict[str, str], RecordingLiquidHandlerDriver]:
    """Run to completion and return the final thread statuses."""
    recorder = RecordingLiquidHandlerDriver(
        ChatterboxLiquidHandlerDriver(num_channels=8))
    build = await _build(recorder, **kwargs)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            "rack_supply", mode=WorkflowRunMode.PURE_SIM)
        statuses = await run_to_quiescence(runtime, record.id, timeout=90.0)
        return statuses, recorder
    finally:
        await runtime.shutdown()


async def _run_twice(
    **kwargs,
) -> tuple[str, list[str], RecordingLiquidHandlerDriver]:
    """Run once to spend the rack, then submit again over the deck it left.

    The bench shape: the rack on the site is real and its ledger says zero.
    Returns the second run's pause message.
    """
    recorder = RecordingLiquidHandlerDriver(
        ChatterboxLiquidHandlerDriver(num_channels=8))
    build = await _build(recorder, **kwargs)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        first = await runtime.submit_workflow(
            "rack_supply", mode=WorkflowRunMode.PURE_SIM)
        await run_to_quiescence(runtime, first.id, timeout=90.0)
        second = await runtime.submit_workflow(
            "rack_supply", mode=WorkflowRunMode.PURE_SIM)
        await wait_for_paused_thread(runtime, second.id, timeout=60.0)
        threads = [t.name for t in runtime.list_threads(second.id)]
        return _pause_messages(runtime, second.id), threads, recorder
    finally:
        await runtime.shutdown()


async def _run_until_refused(**kwargs) -> tuple[str, RecordingLiquidHandlerDriver]:
    """Run until a contributor pauses; return its pause message."""
    recorder = RecordingLiquidHandlerDriver(
        ChatterboxLiquidHandlerDriver(num_channels=8))
    build = await _build(recorder, **kwargs)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            "rack_supply", mode=WorkflowRunMode.PURE_SIM)
        await wait_for_paused_thread(runtime, record.id, timeout=60.0)
        return _pause_messages(runtime, record.id), recorder
    finally:
        await runtime.shutdown()


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_a_second_run_over_a_spent_rack_asks_for_a_replacement() -> None:
    """The bench defect: a rack recorded with zero tips at submit time.

    The first run empties the rack and leaves it on its site. The second run's
    FIRST contribution used to bind it without asking, and the shortfall
    surfaced inside the action body as NotEnoughTips with no replacement asked
    for. It must refuse before anything reaches the instrument, and say what to
    replace.
    """
    message, threads, recorder = await _run_twice(
        tip_positions=_ONE_CONTRIBUTION, with_tips=True, contributions=1)

    assert _picked_positions(recorder) == [["A1", "B1"]], (
        f"only the first run may pick tips: {_picked_positions(recorder)}")
    assert "put a fresh one there" in message, message
    assert "NotEnoughTips" not in message, (
        f"the refusal must come from the capacity gate, not from the action "
        f"body: {message}")
    assert not [t for t in threads if t.startswith("tips")], (
        f"the refusal must land before the bind, so the second run must have "
        f"no tips receiver at all: {threads}")


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_a_rack_with_tips_is_bound_and_the_run_completes() -> None:
    statuses, recorder = await _run(
        tip_positions=None, with_tips=True, contributions=1)

    assert _picked_positions(recorder) == [["A1", "B1"]], (
        f"a full rack must supply the first contribution: "
        f"{_picked_positions(recorder)}")
    assert all(s == "COMPLETED" for s in statuses.values()), statuses


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_a_deck_resident_rack_that_empties_mid_run_is_replaced() -> None:
    """The mid-run half: the spent rack leaves by its declared removal and a
    replacement arrives by its declared spawn. Kept
    here so the first-contribution fix cannot regress it."""
    statuses, recorder = await _run(
        tip_positions=_ONE_CONTRIBUTION, with_tips=True, contributions=2)

    assert len(_picked_positions(recorder)) == 2, (
        f"the spent rack is replaced and the second contribution served: "
        f"{_picked_positions(recorder)}; statuses {statuses}")


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_a_stacker_fed_rack_that_empties_is_replaced() -> None:
    """The overflow path with somewhere to get a rack from.

    A stacker-fed receiver can be replaced without anyone touching the deck, so
    this is the shape the capacity policy was written for. If this stalls the
    problem is the overflow path itself, not the deck-resident lifecycle.
    """
    statuses, recorder = await _run(
        tip_positions=_ONE_CONTRIBUTION, with_tips=True, contributions=2,
        tips_from_stacker=True)

    assert len(_picked_positions(recorder)) == 2, (
        f"the second contribution must be served by a replacement rack: "
        f"{_picked_positions(recorder)}; statuses {statuses}")
