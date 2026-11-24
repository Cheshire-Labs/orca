"""Every way of running a workflow together, on one small system, and the same five checks on each.

Each row picks: how the sample plate is acquired, whether a second submission joins, how many
groups, how the plate's thread ends, and what interrupts the run. The reagent and tip rack are
deck residents every group shares. Every row must complete, start each plate where it is, move
labware only by arm, end each plate where its end kind puts it, end with the right volumes, and
leave nothing on the settle list.
"""
import asyncio
from dataclasses import dataclass
from enum import Enum
from itertools import product

import pytest

import orca.orca as orca
from cheshire_drivers import CartesianCoordinates as C
from cheshire_drivers import Teachpoint
from orca.devices.devices import Storage
from orca.devices.venus import Venus
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.resource_models.transporter import Transporter
from orca.workflow_models.labware_group import (
    Acquisition,
    BarcodeAcquisition,
    LabwareGroup,
    LabwareGroupMember,
    LocationAcquisition,
    PoolAcquisition,
)
from orca.events.event_bus import EventBus
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.labware_service import LabwareService
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.workflow_models.submission_modes import BatchMode
from orca.runtime.startup import STALL_CHECK_INTERVAL_SECONDS
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import SystemBuild, Topology, build_system_over
from orca.sdk.labware import PlateTemplate, TipRackTemplate
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, MANUAL_REMOVE, REUSE_EXISTING
from orca.state.ops_store import IOpsHistoryStore
from orca.state.records import DeclaredTracking, DeclaredVolumeTransfer, LabwareInitialState
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import EndArg, StartArg
from tests.memory_activity import memory_ops_store, memory_runtime, memory_unsaved_positions
from tests.setup_package import PackageSetupStoreFactory, package_stores, with_teachpoints, write_teachpoints
from tests.test_helpers import execution_outcome, state_every_liquid_handler_head_empty, wait_until

_LABWARE_TYPE = "Cor_Falcon_96_wellplate_340ul_Fb_Black"
_GRIPPER = "arm/gripper"
_STACKER_OUTPUT = "stacker/slot"
_WORK_SITE = "lh/sample_site"
_START_PAD = "entry"
_SHELVES = ("shelf_1", "shelf_2", "shelf_3")
_END_PADS = tuple(f"end_{i}" for i in range(6))
_REAGENT_START_UL = 300.0
_TRANSFER_UL = 25.0
_WORKFLOW = "combination_wf"


class Pick(str, Enum):
    POOL = "pool"
    BARCODE = "barcode"
    LOCATION = "location"


class End(str, Enum):
    STORE = "store"
    LEAVE = "leave"
    REMOVE = "remove"


class Interruption(str, Enum):
    NONE = "none"
    PAUSE = "pause"
    RELOAD = "reload"


@dataclass(frozen=True)
class Combination:
    pick: Pick
    joins: bool
    groups: int
    end: End
    interruption: Interruption

    @property
    def plates_per_run(self) -> int:
        return self.groups + (1 if self.joins else 0)

    @property
    def runs(self) -> int:
        return 2 if self.interruption is Interruption.RELOAD else 1

    def __str__(self) -> str:
        return "-".join((
            self.pick, "join" if self.joins else "alone", f"{self.groups}g", self.end, self.interruption,
        ))


_ALL = [Combination(*row) for row in product(Pick, (False, True), (1, 2), End, Interruption)]


@dataclass
class _Lab:
    stores: PackageSetupStoreFactory
    labware: LabwareService
    ops: IOpsHistoryStore
    method_is_running: asyncio.Event
    let_the_method_finish: asyncio.Event


async def _build(
    combo: Combination, lab: _Lab, *, reagent_stacker: str | None = None, parking_pads: tuple[str, ...] | None = None,
    pads_no_arm_reaches: tuple[str, ...] = (),
) -> SystemBuild:
    """``reagent_stacker`` gives each group its own reagent, dispensed from that stacker.
    ``parking_pads`` limits which pads a deadlocked plate may be moved aside to; None leaves every pad.
    ``pads_no_arm_reaches`` adds parking pads the arm has no teachpoint for."""
    group_sharing = GroupSharing.SHARED_ACROSS_GROUPS
    batching = SubmissionBatching.BATCHABLE
    sample = PlateTemplate("sample", labware_type=_LABWARE_TYPE, initial_state=LabwareInitialState(uniform_volume=0.0))
    reagent = PlateTemplate(
        "reagent", labware_type=_LABWARE_TYPE, initial_state=LabwareInitialState(uniform_volume=_REAGENT_START_UL),
        group_sharing=GroupSharing.PER_GROUP if reagent_stacker is not None else group_sharing, submission_batching=batching,
    )
    tips = TipRackTemplate(
        "tips", labware_type="hamilton_96_tiprack_10uL_filter", with_tips=True,
        group_sharing=group_sharing, submission_batching=batching,
    )
    lh = Venus("lh", site_names=["sample_site", "reagent_site", "tips_site"])
    points = ["stacker", "reagents", _START_PAD, *_SHELVES, *_END_PADS, "done", "lh/sample_site", "lh/reagent_site"]
    arm = with_teachpoints(Transporter("arm"), write_teachpoints(lab.stores, "arm", [
        Teachpoint(name, C(100 * i, 200, 300, 0, 90, 180), orientation="right") for i, name in enumerate(points)
    ]))

    @orca.action(
        device=lh, inputs=[sample, reagent, tips],
        deck_positions={sample: "sample_site", reagent: "reagent_site", tips: "tips_site"},
        declares=DeclaredTracking(volume_transferred=[DeclaredVolumeTransfer(
            source="reagent", target="sample", volume_ul=_TRANSFER_UL, source_wells=["A1"], target_wells=["A1"],
        )]),
    )
    async def add_reagent(ctx: ActionContext) -> None:
        lab.method_is_running.set()
        await lab.let_the_method_finish.wait()
        await ctx.device().run_protocol("AddReagent.hsl", {})

    @orca.method
    async def add(ctx: MethodContext):
        yield add_reagent

    start = ("stacker", DISPENSE) if combo.pick is Pick.POOL else _START_PAD
    ends: dict[End, EndArg] = {
        End.STORE: "done", End.LEAVE: (list(_END_PADS), LEAVE_IN_PLACE), End.REMOVE: ("done", MANUAL_REMOVE),
    }

    @orca.thread(labware=sample, start=start, end=ends[combo.end])
    async def sample_journey(ctx: ThreadContext):
        yield add

    reagent_start: StartArg = (reagent_stacker, DISPENSE) if reagent_stacker is not None else ("lh/reagent_site", REUSE_EXISTING)
    reagent_end: EndArg = "done" if reagent_stacker is not None else ("lh/reagent_site", LEAVE_IN_PLACE)

    @orca.thread(labware=reagent, start=reagent_start, end=reagent_end)
    async def reagent_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[add])

    @orca.thread(labware=tips, start=("lh/tips_site", REUSE_EXISTING), end=("lh/tips_site", LEAVE_IN_PLACE))
    async def tips_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[add])

    @orca.workflow(name=_WORKFLOW)
    def workflow(wf):
        wf.start(sample_journey)
        wf.thread(reagent_journey)
        wf.thread(tips_journey)

    topology = Topology(
        locations={
            "stacker": Storage("stacker"), "reagents": Storage("reagents"), "done": Storage("done"), "lh": lh,
            **{
                pad: PlatePad(pad, supports_deadlock_resolution=parking_pads is None or pad in parking_pads)
                for pad in (_START_PAD, *_SHELVES, *_END_PADS)
            },
            **{pad: PlatePad(pad, supports_deadlock_resolution=True) for pad in pads_no_arm_reaches},
        },
        transporters=[arm],
    )
    return await build_system_over(
        name="Combinations", workflow=workflow, topology=topology, stores=lab.stores,
        event_bus=EventBus(), ops_history_store=lab.ops,
        labware_service=lab.labware, unsaved_positions=memory_unsaved_positions(),
    )


async def _started(
    combo: Combination, lab: _Lab, *, after_reload: bool = False, reagent_stacker: str | None = None,
    parking_pads: tuple[str, ...] | None = None, stall_check_interval: float = STALL_CHECK_INTERVAL_SECONDS,
    pads_no_arm_reaches: tuple[str, ...] = (),
) -> tuple[SystemBuild, SystemRuntime]:
    build = await _build(
        combo, lab, reagent_stacker=reagent_stacker, parking_pads=parking_pads, pads_no_arm_reaches=pads_no_arm_reaches,
    )
    runtime = await memory_runtime(
        build.system, event_bus=build.event_bus, stall_check_interval=stall_check_interval,
    )
    await runtime.start(after_reload=after_reload)
    await state_every_liquid_handler_head_empty(runtime)
    return build, runtime


def _spot_of_plate(index: int) -> str:
    """The first plate already sits at the start of its thread; the others have to be carried there."""
    return _START_PAD if index == 0 else _SHELVES[index - 1]


async def _put_plates_on_the_deck(combo: Combination, runtime: SystemRuntime, run: int) -> dict[str, str]:
    """Returns labware id -> where it was put."""
    if combo.pick is not Pick.BARCODE:
        return {}
    placed: dict[str, str] = {}
    for index in range(combo.plates_per_run):
        spot = _spot_of_plate(index)
        snapshot = await runtime.labware.register("sample", location=spot, barcode=f"S{run}-{index}", confirm=True)
        placed[snapshot.id] = spot
    return placed


def _acquisition(combo: Combination, run: int, index: int) -> Acquisition:
    if combo.pick is Pick.BARCODE:
        return BarcodeAcquisition(barcode=f"S{run}-{index}")
    if combo.pick is Pick.LOCATION:
        return LocationAcquisition(source_location=_SHELVES[index])
    return PoolAcquisition()


def _groups(combo: Combination, run: int, indexes: range) -> list[LabwareGroup]:
    return [
        LabwareGroup(id=f"r{run}p{i}", members=(LabwareGroupMember("sample_journey", _acquisition(combo, run, i)),))
        for i in indexes
    ]


async def _pause_during_a_method(lab: _Lab, runtime: SystemRuntime, execution_id: str) -> None:
    await asyncio.wait_for(lab.method_is_running.wait(), timeout=10.0)
    runtime.pause_execution(execution_id)
    lab.let_the_method_finish.set()
    await wait_until(
        lambda: any(t.status == "PAUSED" for t in runtime.list_threads(execution_id)),
        timeout=10.0, message="the pause never reached a thread",
    )
    runtime.resume_execution(execution_id)


def _labware_of(runtime: SystemRuntime, execution_id: str, thread_prefix: str) -> set[str]:
    return {
        t.labware_id for t in runtime.list_threads(execution_id)
        if t.labware_id is not None and t.name.startswith(thread_prefix)
    }


def _route_of_a_plate_put_at(spot: str) -> list[str]:
    """A plate already at the start goes straight to work; any other is carried to the start first."""
    to_start = [] if spot == _START_PAD else [_START_PAD, _GRIPPER]
    return [spot, _GRIPPER, *to_start, _WORK_SITE]


def _first_positions_expected(combo: Combination) -> list[str]:
    if combo.pick is Pick.POOL:
        return [_STACKER_OUTPUT] * combo.plates_per_run
    if combo.pick is Pick.LOCATION:
        return sorted(_SHELVES[:combo.plates_per_run])
    return sorted(_spot_of_plate(i) for i in range(combo.plates_per_run))


def _assert_each_plate_ended_where_its_end_kind_puts_it(combo: Combination, last_positions: list[str]) -> None:
    """A stored or removed plate is carried to the store; each plate left in place takes an end pad of its own."""
    if combo.end is End.LEAVE:
        assert all(spot in _END_PADS for spot in last_positions), last_positions
        assert len(set(last_positions)) == len(last_positions), last_positions
    else:
        assert last_positions == ["done/slot"] * combo.plates_per_run, last_positions


async def _run_once(lab: _Lab, combo: Combination, build: SystemBuild, runtime: SystemRuntime, run: int) -> set[str]:
    template = build.system.get_workflow_template(_WORKFLOW)
    placed = await _put_plates_on_the_deck(combo, runtime, run)
    batch = BatchMode.JOIN_EXISTING if combo.joins else BatchMode.STANDALONE
    submission = await runtime.submit(
        template, groups=_groups(combo, run, range(combo.groups)), batch_mode=batch, mode=WorkflowRunMode.SIM,
    )
    await asyncio.wait_for(runtime._executions[submission.execution_id].workflow_attached.wait(), timeout=10.0)
    if combo.joins:
        await runtime.submit(
            template, groups=_groups(combo, run, range(combo.groups, combo.plates_per_run)),
            batch_mode=batch, mode=WorkflowRunMode.SIM,
        )
    if combo.interruption is Interruption.PAUSE:
        await _pause_during_a_method(lab, runtime, submission.execution_id)
    outcome = await execution_outcome(runtime, submission, timeout=30.0)
    assert outcome.status == "completed", outcome
    samples = _labware_of(runtime, submission.execution_id, "sample")
    assert len(samples) == combo.plates_per_run
    routes = {sample: await _positions_moved_only_by_arm(runtime, sample) for sample in samples}
    assert sorted(route[0] for route in routes.values()) == _first_positions_expected(combo)
    for sample in samples:
        assert await _well(runtime, sample) == _TRANSFER_UL
    _assert_each_plate_ended_where_its_end_kind_puts_it(combo, [route[-1] for route in routes.values()])
    for labware_id, spot in placed.items():
        expected = _route_of_a_plate_put_at(spot)
        assert routes[labware_id][:len(expected)] == expected
    return _labware_of(runtime, submission.execution_id, "reagent")


async def _positions_moved_only_by_arm(runtime: SystemRuntime, labware_id: str) -> list[str]:
    positions = [entry.position_id for entry in await runtime.labware.get_history(labware_id)]
    for here, there in zip(positions, positions[1:]):
        assert _GRIPPER in (here, there), f"{here} -> {there} was not an arm move: {positions}"
    return positions


async def _well(runtime: SystemRuntime, labware_id: str) -> float:
    return (await runtime.labware.get_well_volumes(labware_id)).volumes.get("A1", 0.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("combo", _ALL, ids=str)
async def test_the_combination_runs_and_leaves_a_true_record(combo: Combination) -> None:
    lab = _Lab(package_stores(), LabwareService(InMemoryLabwareStore()), memory_ops_store(), asyncio.Event(), asyncio.Event())
    if combo.interruption is not Interruption.PAUSE:
        lab.let_the_method_finish.set()
    build, runtime = await _started(combo, lab)
    try:
        reagents = await _run_once(lab, combo, build, runtime, run=0)
        if combo.interruption is Interruption.RELOAD:
            await runtime.shutdown()
            build, runtime = await _started(combo, lab, after_reload=True)
            reagents |= await _run_once(lab, combo, build, runtime, run=1)
        used = [_REAGENT_START_UL - await _well(runtime, reagent) for reagent in reagents]
        assert sum(used) == _TRANSFER_UL * combo.plates_per_run * combo.runs
        assert await runtime.unsettled_state() == []
    finally:
        await runtime.shutdown(confirm=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("combo", [
    Combination(Pick.POOL, False, 2, End.STORE, Interruption.NONE),
    Combination(Pick.POOL, True, 1, End.STORE, Interruption.NONE),
], ids=str)
async def test_a_per_group_reagent_from_a_stacker_leaves_the_site_for_the_next_groups_reagent(combo: Combination) -> None:
    """Each group gets its own reagent from the stacker on the one reagent site; the first leaves for the second."""
    lab = _Lab(package_stores(), LabwareService(InMemoryLabwareStore()), memory_ops_store(), asyncio.Event(), asyncio.Event())
    lab.let_the_method_finish.set()
    build, runtime = await _started(combo, lab, reagent_stacker="reagents")
    try:
        reagents = await _run_once(lab, combo, build, runtime, run=0)
        assert len(reagents) == 2
        assert [_REAGENT_START_UL - await _well(runtime, reagent) for reagent in reagents] == [_TRANSFER_UL] * 2
    finally:
        await runtime.shutdown(confirm=True)
