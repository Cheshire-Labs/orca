"""A sample plate and a per-group reagent plate both start with DISPENSE from one stacker.

The first sample takes the liquid handler and waits for its reagent. The next sample is already
in the stacker output, waiting for the handler, so the reagent cannot come out. That is a
deadlock. The plate in the output moves aside to a pad, the output is kept for the reagent, and
the run finishes. One plate moves per deadlock.
"""
import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from orca.modes.run_modes import WorkflowRunMode
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.runtime.labware_service import LabwareService
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.stall_detector import SystemStallError
from orca.runtime.startup import STALL_CHECK_INTERVAL_SECONDS
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import SystemBuild
from orca.system.reservation_manager.location_reservation import LocationReservation
from orca.workflow_models.actions.move_handler import MoveHandler
from tests.memory_activity import memory_ops_store
from tests.setup_package import package_stores
from tests.test_helpers import execution_outcome, wait_until
from tests.test_workflow_combinations import (
    _END_PADS,
    _REAGENT_START_UL,
    _SHELVES,
    _STACKER_OUTPUT,
    _START_PAD,
    _TRANSFER_UL,
    _WORKFLOW,
    Combination,
    End,
    Interruption,
    Pick,
    _groups,
    _Lab,
    _run_once,
    _started,
    _well,
)

_PADS = (_START_PAD, *_SHELVES, *_END_PADS)
_QUICK_STALL_CHECK_S = 0.25
_SEVERAL_RESERVATION_RETRIES_S = 3.0


@dataclass
class _ArmLog:
    """Every carry the arm made, in order: which plate, from where, to where."""
    carries: list[tuple[str, str, str]] = field(default_factory=list)
    never_put_down_on_a_pad: bool = False
    force_the_deadlock: bool = True
    carrying_a_plate_aside: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def out_of_the_stacker(self) -> list[tuple[str, str]]:
        """(plate template, destination) of each plate carried out of the stacker output, in order."""
        return [(plate.split("-")[0], to) for plate, source, to in self.carries if source == _STACKER_OUTPUT]

    @property
    def moved_aside(self) -> list[str]:
        return [plate for plate, source, to in self.carries if source == _STACKER_OUTPUT and to in _PADS]


@contextmanager
def _the_shared_stacker_deadlock(runtime: SystemRuntime) -> Iterator[_ArmLog]:
    """Holds the first reagent's request for the stacker output until the second sample is in it,
    so the first reagent always finds the output full. Logs every carry."""
    arm = runtime.system.get_transporter("arm")
    output = runtime.system.get_location(_STACKER_OUTPUT)
    pick, place = arm._do_pick, arm._do_place
    acquire = MoveHandler.acquire_placement_reservation
    log = _ArmLog()
    carrying: list[tuple[str, str]] = []
    first_sample: list[str] = []
    first_reagent = True

    def the_second_sample_is_out() -> bool:
        standing = output.labware
        return bool(first_sample) and standing is not None and standing.name.startswith("sample") and standing.name != first_sample[0]

    async def held_acquire(
        handler: MoveHandler, thread_id: str, labware: LabwareInstance, location: Location,
    ) -> LocationReservation:
        nonlocal first_reagent
        if log.force_the_deadlock and first_reagent and labware.name.startswith("reagent"):
            first_reagent = False
            await wait_until(the_second_sample_is_out, timeout=10.0, message="the second sample never came out")
        return await acquire(handler, thread_id, labware, location)

    async def logged_pick(location: Location) -> None:
        assert location.labware is not None
        if not first_sample and location.position_id == _STACKER_OUTPUT:
            first_sample.append(location.labware.name)
        carrying.append((location.labware.name, location.position_id))
        await pick(location)

    async def logged_place(location: Location) -> None:
        if log.never_put_down_on_a_pad and location.position_id in _PADS:
            log.carrying_a_plate_aside.set()
            await asyncio.Event().wait()
        await place(location)
        plate, source = carrying.pop()
        log.carries.append((plate, source, location.position_id))

    with (
        patch.object(arm, "_do_pick", logged_pick),
        patch.object(arm, "_do_place", logged_place),
        patch.object(MoveHandler, "acquire_placement_reservation", held_acquire),
    ):
        yield log


def _shared_stacker(groups: int) -> Combination:
    return Combination(Pick.POOL, False, groups, End.STORE, Interruption.NONE)


def _new_lab() -> _Lab:
    lab = _Lab(package_stores(), LabwareService(InMemoryLabwareStore()), memory_ops_store(), asyncio.Event(), asyncio.Event())
    lab.let_the_method_finish.set()
    return lab


async def _started_on_one_stacker(
    groups: int, *, parking_pads: tuple[str, ...] | None = None, pads_no_arm_reaches: tuple[str, ...] = (),
    lab: _Lab | None = None, after_reload: bool = False, stall_check_interval: float = STALL_CHECK_INTERVAL_SECONDS,
) -> tuple[_Lab, SystemBuild, SystemRuntime]:
    lab = lab if lab is not None else _new_lab()
    build, runtime = await _started(
        _shared_stacker(groups), lab, reagent_stacker="stacker", after_reload=after_reload,
        stall_check_interval=stall_check_interval, parking_pads=parking_pads, pads_no_arm_reaches=pads_no_arm_reaches,
    )
    return lab, build, runtime


def _assert_one_plate_moved_per_deadlock_and_the_reagent_came_out_next(log: _ArmLog, groups: int) -> None:
    out = log.out_of_the_stacker
    moved = log.moved_aside
    assert len(moved) == len(set(moved)), f"a plate was moved aside twice: {log.carries}"
    assert 1 <= len(moved) <= groups - 1, f"{len(moved)} plates moved aside for {groups} groups: {log.carries}"
    for index, (template, destination) in enumerate(out):
        if destination in _PADS:
            assert template == "sample", out
            assert out[index + 1][0] == "reagent", f"the freed output did not go to the reagent: {out}"


@pytest.mark.asyncio
@pytest.mark.parametrize("groups", [2, 3, 4, 5])
async def test_every_group_finishes_and_one_plate_moves_aside_per_deadlock(groups: int) -> None:
    lab, build, runtime = await _started_on_one_stacker(groups)
    try:
        with _the_shared_stacker_deadlock(runtime) as log:
            reagents = await _run_once(lab, _shared_stacker(groups), build, runtime, run=0)
        assert len(reagents) == groups
        assert [_REAGENT_START_UL - await _well(runtime, reagent) for reagent in reagents] == [_TRANSFER_UL] * groups
        _assert_one_plate_moved_per_deadlock_and_the_reagent_came_out_next(log, groups)
        assert await runtime.unsettled_state() == []
    finally:
        await runtime.shutdown(confirm=True)


async def _the_run_pauses_as_a_stall(build: SystemBuild, runtime: SystemRuntime) -> None:
    template = build.system.get_workflow_template(_WORKFLOW)
    with _the_shared_stacker_deadlock(runtime) as log:
        submission = await runtime.submit(template, groups=_groups(_shared_stacker(2), 0, range(2)), mode=WorkflowRunMode.SIM)
        with pytest.raises(SystemStallError):
            await execution_outcome(runtime, submission, timeout=30.0)
    assert log.moved_aside == []


@pytest.mark.asyncio
async def test_with_no_pad_to_move_aside_to_the_run_pauses() -> None:
    _, build, runtime = await _started_on_one_stacker(2, parking_pads=(), stall_check_interval=_QUICK_STALL_CHECK_S)
    try:
        await _the_run_pauses_as_a_stall(build, runtime)
    finally:
        await runtime.shutdown(confirm=True)


@pytest.mark.asyncio
async def test_with_only_a_pad_no_arm_reaches_the_run_pauses() -> None:
    _, build, runtime = await _started_on_one_stacker(
        2, parking_pads=(), pads_no_arm_reaches=("island",), stall_check_interval=_QUICK_STALL_CHECK_S,
    )
    try:
        await _the_run_pauses_as_a_stall(build, runtime)
    finally:
        await runtime.shutdown(confirm=True)


@pytest.mark.asyncio
async def test_a_reload_while_the_plate_is_carried_aside_leaves_the_next_run_working() -> None:
    lab, build, runtime = await _started_on_one_stacker(2)
    try:
        template = build.system.get_workflow_template(_WORKFLOW)
        with _the_shared_stacker_deadlock(runtime) as log:
            log.never_put_down_on_a_pad = True
            await runtime.submit(template, groups=_groups(_shared_stacker(2), 0, range(2)), mode=WorkflowRunMode.SIM)
            await asyncio.wait_for(log.carrying_a_plate_aside.wait(), timeout=20.0)
            await runtime.shutdown()
        _, build, runtime = await _started_on_one_stacker(2, lab=lab, after_reload=True)
        await runtime.labware.clear_all_labware(reason="the interrupted run's plates were taken off the deck", confirm=True)
        # The shutdown can cancel a dispense, and a fresh run refuses the faulted stacker until someone looks at it.
        await runtime.devices.clear_fault("stacker", reason="the operator looked at the stacker", confirm=True)
        with _the_shared_stacker_deadlock(runtime) as log:
            reagents = await _run_once(lab, _shared_stacker(2), build, runtime, run=1)
        assert len(reagents) == 2
        _assert_one_plate_moved_per_deadlock_and_the_reagent_came_out_next(log, 2)
    finally:
        await runtime.shutdown(confirm=True)


@pytest.mark.asyncio
async def test_a_sample_waiting_for_a_handler_another_execution_is_using_is_left_in_the_output() -> None:
    """The other execution's action is running, so the wait is slow, not stuck: nothing moves aside."""
    lab, build, runtime = await _started_on_one_stacker(1)
    lab.let_the_method_finish.clear()
    output = runtime.system.get_location(_STACKER_OUTPUT)
    try:
        template = build.system.get_workflow_template(_WORKFLOW)
        with _the_shared_stacker_deadlock(runtime) as log:
            log.force_the_deadlock = False
            first = await runtime.submit(template, groups=_groups(_shared_stacker(1), 0, range(1)), mode=WorkflowRunMode.SIM)
            await asyncio.wait_for(lab.method_is_running.wait(), timeout=20.0)
            second = await runtime.submit(template, groups=_groups(_shared_stacker(1), 1, range(1)), mode=WorkflowRunMode.SIM)
            await wait_until(lambda: output.labware is not None, timeout=10.0, message="the second sample never came out")
            await asyncio.sleep(_SEVERAL_RESERVATION_RETRIES_S)
            assert log.moved_aside == []
            lab.let_the_method_finish.set()
            for submission in (first, second):
                assert (await execution_outcome(runtime, submission, timeout=30.0)).status == "completed"
        assert log.moved_aside == []
    finally:
        await runtime.shutdown(confirm=True)


@pytest.mark.asyncio
async def test_a_deadlock_across_two_executions_is_broken_the_same_way() -> None:
    """One execution's sample holds the handler and waits for its reagent. The other execution's
    sample fills the output."""
    lab, build, runtime = await _started_on_one_stacker(1)
    try:
        template = build.system.get_workflow_template(_WORKFLOW)
        with _the_shared_stacker_deadlock(runtime) as log:
            submissions = [
                await runtime.submit(template, groups=_groups(_shared_stacker(1), run, range(1)), mode=WorkflowRunMode.SIM)
                for run in range(2)
            ]
            assert submissions[0].execution_id != submissions[1].execution_id
            for submission in submissions:
                assert (await execution_outcome(runtime, submission, timeout=30.0)).status == "completed"
        _assert_one_plate_moved_per_deadlock_and_the_reagent_came_out_next(log, 2)
    finally:
        await runtime.shutdown(confirm=True)
