"""Asking a person to put a plate somewhere is a claim on the spot.

From the bench, 2026-09-02. A thread had been parked at AWAITING_MANUAL_PLACE
on pad_1 for sixteen minutes when the engine granted that same pad_1 to another
thread's move home. A second later the operator did what the first surface had
asked and registered a plate there, and the returning plate arrived to find the
pad taken. Nothing detected the cycle; the afternoon went to unpicking it.

An AWAITING_MANUAL_PLACE expectation held no claim on its location. That is the
root: the engine could promise a slot to one thread while asking a person to
fill it for another. The sim half of the same spawn has always taken the claim,
over a much shorter wait, for exactly this reason.

The claim alone would be half a fix. It leaves an operator holding two
contradictory instructions the moment a claim goes the other way, so every verb
that states a position refuses one somebody else has claimed and names the
holder (``test_edit_location_forces_a_re_resolve`` covers the other two). The
verb that FULFILS a wait is never refused for the claim that wait is holding, or
the plate an operator was asked for could never be put down.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.transporter import Transporter
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.registries import NullGatewayRegistry
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.reservation_manager.location_reservation import (
    ReservationPriority,
)
from orca.system.reservation_manager.reservation_manager import (
    LocationReservationManager,
)
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.status_enums import LabwareThreadStatus, RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from tests.runtime.manual_place_fixtures import (
    _live_connection_source,
)
from tests.test_helpers import (
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    wait_until,
    wire_system_map,
)

PADS = ["pad1", "pad2"]
THREAD_NAME = "journey"


@dataclass
class Bench:
    runtime: SystemRuntime
    workflow: WorkflowTemplate
    reservations: LocationReservationManager
    release_the_shake: asyncio.Event
    """Held closed by ``hold_at_the_shaker``, so a test can say exactly when the
    first plate is allowed to start its journey home."""


async def _build_bench(
    *, end_pad: str = "pad1", hold_at_the_shaker: bool = False,
    arm: Transporter | None = None,
) -> Bench:
    """One shaker, two pads, and a thread that is handed its plate at pad1.

    ``end_pad="pad1"`` is the shape of the bench workflow that produced the
    incident: one pad is both where a plate is handed in and where it goes back.

    ``arm`` takes a transporter built by the caller, for a test that needs to
    stop the arm somewhere; it must reach the shaker and both pads.
    """
    device = create_test_device("shaker1")
    if arm is None:
        arm = create_test_transporter("robot1", ["shaker1", *PADS])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(arm)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=PADS)

    release_the_shake = asyncio.Event()
    if not hold_at_the_shaker:
        release_the_shake.set()

    @orca.action(device=pool, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        await release_the_shake.wait()
        await ctx.device().shake(duration=1, speed=500)

    async def _method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake

    method = MethodTemplate("shake_method", func=_method)

    async def journey(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield method

    workflow = WorkflowTemplate("shared_pad_workflow")
    workflow.add_thread(
        ThreadTemplate(
            labware_template=plate,
            start=system_map.get_location("pad1"),
            end=system_map.get_location(end_pad),
            func=journey,
        ),
        is_start=True,
    )

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="shared_pad_system", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    return Bench(
        runtime=SystemRuntime(
            builder.get_system(),
            labware_store=InMemoryLabwareStore(),
            event_bus=event_bus,
            gateway_registry=NullGatewayRegistry(),
            connection_source=_live_connection_source(),
        ),
        workflow=workflow,
        reservations=builder._thread_reservation_coordinator._reservation_manager,
        release_the_shake=release_the_shake,
    )


async def _submit(bench: Bench, *, groups: int = 1) -> str:
    """A LIVE submission of ``groups`` plates through the one thread template.

    Groups are how the bench workflow ran two source plates through one journey:
    one submission, two lineages, both waiting at the same pad.
    """
    submission = await bench.runtime.submit(
        bench.workflow,
        groups=[
            LabwareGroup(
                id=f"g{index}",
                members=[LabwareGroupMember(thread_template_name=THREAD_NAME)],
            )
            for index in range(groups)
        ],
        mode=WorkflowRunMode.LIVE,
    )
    return submission.execution_id


def _claim_holder(bench: Bench) -> str | None:
    """The thread holding pad1, or None when nothing holds it."""
    held = bench.reservations.get_reservation_at("pad1")
    return held.thread_id if held is not None else None


def _threads_with_status(bench: Bench, execution_id: str, status: str) -> list[str]:
    return [
        t.id for t in bench.runtime.list_threads(execution_id)
        if t.status == status
    ]


async def _wait_for_a_parked_wait(
    bench: Bench, execution_id: str, *, count: int = 1,
) -> list[str]:
    """Thread ids parked for an operator, once the pad has actually been claimed.

    Both halves: the status is published before the spawn runs, so a test that
    reads the claim has to wait for the claim and not just for the status.
    """
    parked = LabwareThreadStatus.AWAITING_MANUAL_PLACE.value
    await wait_until(
        lambda: len(_threads_with_status(bench, execution_id, parked)) >= count,
        timeout=30.0,
        message=f"{count} thread(s) never parked at AWAITING_MANUAL_PLACE",
    )
    await wait_until(
        lambda: bench.reservations.get_reservation_at("pad1") is not None,
        timeout=15.0,
        message="the operator wait never claimed the pad it was waiting on",
    )
    return _threads_with_status(bench, execution_id, parked)


async def _pad_is_free(bench: Bench, position_id: str, *, timeout: float) -> None:
    await wait_until(
        lambda: bench.runtime.system.system_map.get_location(position_id).labware
        is None,
        timeout=timeout,
        message=f"{position_id} never cleared",
    )


async def _collect_finished_plates(bench: Bench, execution_id: str) -> dict[str, str]:
    """Take every plate the run parks for collection, and report where the
    threads ended up.

    A LIVE thread that ends on an operator removal waits for a person, so a LIVE
    run only finishes if somebody keeps picking plates up. Threads rather than
    the execution: a grouped submission keeps its execution ACCEPTING until
    somebody closes it, which is a different question from whether the plates
    got through.
    """
    awaiting = LabwareThreadStatus.AWAITING_MANUAL_REMOVE.value
    terminal = {"COMPLETED", "ABORTED", "STOPPED", "FAILED"}

    async def _done() -> bool:
        statuses = []
        for thread in bench.runtime.list_threads(execution_id):
            if thread.status == awaiting and thread.labware_id is not None:
                await bench.runtime.labware.discharge_labware(thread.labware_id)
            statuses.append(thread.status)
        return bool(statuses) and all(s in terminal for s in statuses)

    try:
        await wait_until(_done, timeout=90.0)
    except TimeoutError:
        states = [
            f"{t.name}[{t.status}]"
            for t in bench.runtime.list_threads(execution_id)
        ]
        raise AssertionError(
            f"the run never reached a terminal state; threads: {states}"
        ) from None
    return {t.name: t.status for t in bench.runtime.list_threads(execution_id)}


async def _abandon(bench: Bench, execution_id: str) -> None:
    for thread in bench.runtime.list_threads(execution_id):
        with contextlib.suppress(Exception):
            bench.runtime.recover_thread(
                execution_id, thread.id, RecoveryDecision.ABORT_THREAD,
            )
    with contextlib.suppress(Exception):
        await asyncio.wait_for(bench.runtime.wait(execution_id), timeout=20.0)


class TestTheWaitHoldsTheSpot:

    async def test_a_thread_awaiting_a_place_holds_the_pad(self) -> None:
        """The root cause, stated at the layer that grants: while a person is
        being asked to fill pad1, pad1 is claimed.

        The claim keeps the pad from everything except a plate that needs
        somewhere to land, which outranks it -- see
        ``test_a_plate_going_home_outranks_an_expectation``.
        """
        bench = await _build_bench()
        await bench.runtime.start()
        try:
            execution_id = await _submit(bench)
            parked = await _wait_for_a_parked_wait(bench, execution_id)

            held = bench.reservations.get_reservation_at("pad1")
            assert held is not None
            assert held.thread_id == parked[0]
            assert not bench.reservations.can_reserve(
                "pad1", thread_id="some-other-thread",
                requesting_priority=ReservationPriority.AWAITING_OPERATOR,
            ), "a second operator wait took a pad already claimed for one"
            assert not bench.reservations.can_reserve(
                "pad1", thread_id="some-other-thread",
            ), "an ordinary hold took a pad somebody was asked to fill"
            assert bench.reservations.can_reserve(
                "pad1", thread_id="some-other-thread",
                requesting_priority=ReservationPriority.MOVE_TARGET,
            ), "a plate on its way here was refused a pad nothing stands on"

            await _abandon(bench, execution_id)
        finally:
            await bench.runtime.shutdown()

    async def test_the_claim_is_given_up_once_the_plate_is_there(self) -> None:
        """A claim held past the arrival keeps the pad off everyone else for the
        rest of the run. Physical occupancy guards it from the arrival on."""
        bench = await _build_bench(end_pad="pad2")
        await bench.runtime.start()
        try:
            execution_id = await _submit(bench)
            await _wait_for_a_parked_wait(bench, execution_id)

            await bench.runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )

            await wait_until(
                lambda: bench.reservations.get_reservation_at("pad1") is None,
                timeout=30.0,
                message="the finished wait never released the pad it held",
            )
            await _abandon(bench, execution_id)
        finally:
            await bench.runtime.shutdown()


class TestTheOperatorCanStillPutThePlateDown:

    async def test_the_place_the_wait_asked_for_is_never_refused(self) -> None:
        """The claim on pad1 belongs to the thread whose expectation this
        register adopts. It is not a competing claim: it is the same request,
        so refusing it would leave the operator with nothing they could do."""
        bench = await _build_bench(end_pad="pad2")
        await bench.runtime.start()
        try:
            execution_id = await _submit(bench)
            await _wait_for_a_parked_wait(bench, execution_id)

            snapshot = await bench.runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )

            assert snapshot.current_location == "pad1"
            statuses = await _collect_finished_plates(bench, execution_id)
            assert set(statuses.values()) == {"COMPLETED"}, statuses
        finally:
            await bench.runtime.shutdown()

    async def test_the_second_plates_wait_keeps_the_pad_off_the_first_ones_way_home(
        self,
    ) -> None:
        """The bench incident, inverted.

        Two lineages, one pad, and that pad is both where a plate is handed in
        and where it goes back. The engine granted the pad to the first plate's
        move home while a person was still being asked to put the second plate
        on it, and the two collided one second apart.

        The pad goes back to the waiting thread the moment the first plate
        leaves it, so the move home has to wait its turn, and the operator can
        still do the thing they were asked for.
        """
        bench = await _build_bench(hold_at_the_shaker=True)
        await bench.runtime.start()
        try:
            execution_id = await _submit(bench, groups=2)
            parked = await _wait_for_a_parked_wait(bench, execution_id, count=2)
            first_holder = bench.reservations.get_reservation_at("pad1")
            assert first_holder is not None

            await bench.runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )
            await _pad_is_free(bench, "pad1", timeout=60.0)

            await wait_until(
                lambda: _claim_holder(bench) not in (None, first_holder.thread_id),
                timeout=30.0,
                message="the still-waiting thread never got the pad back",
            )
            assert _claim_holder(bench) in parked, (
                "pad1 went to something other than the thread still waiting on it"
            )

            # The point: this must NOT raise. The claim on pad1 belongs to the
            # thread this register fulfils. The first plate is still held at
            # the shaker, so nothing is on its way to pad1 to outrank it.
            second = await bench.runtime.labware.register(
                "plate_96", location="pad1", confirm=True,
            )
            assert second.current_location == "pad1"

            bench.release_the_shake.set()

            await _abandon(bench, execution_id)
        finally:
            bench.release_the_shake.set()
            await bench.runtime.shutdown()


class TestTheClaimSurvivesTheOperatorsOwnTools:

    async def test_a_cancelled_claim_is_taken_again(self) -> None:
        """`reservation cancel` is what the refusal message tells an operator to
        run, and it takes the claim out of the manager without the holder doing
        anything. A wait that noticed nothing would go on showing
        AWAITING_MANUAL_PLACE while holding no spot at all, which is the bug
        this whole change is about, reached through a documented action.
        """
        bench = await _build_bench()
        await bench.runtime.start()
        try:
            execution_id = await _submit(bench)
            parked = await _wait_for_a_parked_wait(bench, execution_id)
            first = bench.reservations.get_reservation_at("pad1")
            assert first is not None

            bench.reservations.release_reservation_by_id(first.id)
            assert bench.reservations.get_reservation_at("pad1") is None

            await wait_until(
                lambda: bench.reservations.get_reservation_at("pad1") is not None,
                timeout=15.0,
                message="the wait never took its pad back after the cancel",
            )
            retaken = bench.reservations.get_reservation_at("pad1")
            assert retaken is not None
            assert retaken.id != first.id
            assert retaken.thread_id == parked[0]

            await _abandon(bench, execution_id)
        finally:
            await bench.runtime.shutdown()
