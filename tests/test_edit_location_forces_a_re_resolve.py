"""An operator who moves a plate by hand makes the engine plan the move again.

A move's route is planned from where the plate was when the plan was made, and
the grant that lets it run can be hours later, so an operator has every chance
to move the plate in between. Before this, the two ways out of the pause that
followed were both wrong: RETRY re-ran the plan against a source the plate had
left, forever, and CONTINUE recorded the plate at a target nobody had carried
it to.

The thread decides by asking the ledger where its labware is, not by watching
for an edit: restating a position the plate already had is not a move, and a
plate in the jaws or already at the target is a plan mid-flight, not a stale
one.

The operator is never refused for using the labware. They are refused only when
the target could not hold the plate anyway: another plate is already there, or
another thread has reserved it and is on its way.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import pytest

import orca.orca as orca
from orca.resource_models.device_error import SlotOccupiedError
from orca.resource_models.location import Location
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.transporter import Transporter
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.runtime_interface import LocationReservedError
from orca.runtime.status_models import LabwareSnapshot
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.system.reservation_manager.reservation_manager import (
    LocationReservationManager,
)
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import ActionTemplate, IMethodTemplate
from orca.workflow_models.status_enums import RecoveryDecision
from orca.workflow_models.thread_context import ThreadContext
from tests.mock import UniversalMockDevice
from tests.test_helpers import (
    create_test_plate_template,
    create_test_teachpoints,
    seeded_teachpoint_service,
    wait_for_paused_thread,
    wait_until,
    wire_system_map,
)

PADS = ["pad1", "pad2", "pad3"]
# The shaker owns a deck site; a place lands on the site, not the device.
SHAKER_SITE = "shaker1/slot"
# On the map, out of the arm's teachpoints: a position nothing can reach.
STRANDED = "pad_no_arm_reaches"


class _ShakerThatRemembers(UniversalMockDevice):
    """Records every shake so a test can say whether the action ever ran."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.shakes: list[tuple[int, int]] = []

    async def shake(self, duration: int, speed: int) -> None:
        self.shakes.append((duration, speed))
        await super().shake(duration=duration, speed=speed)


class _ArmThatJams(Transporter):
    """Records where it picked from and jams on the place at one position."""

    def __init__(self, name: str, position_ids: list[str]) -> None:
        teachpoints = create_test_teachpoints(position_ids)
        super().__init__(
            name, teachpoint_store=seeded_teachpoint_service(teachpoints),
        )
        self.pick_sources: list[str] = []
        self.place_targets: list[str] = []
        self.jam_at: str | None = None

    async def pick(self, location: Location) -> None:
        self.pick_sources.append(location.position_id)
        await super().pick(location)

    async def place(self, location: Location) -> None:
        self.place_targets.append(location.position_id)
        if self.jam_at == location.position_id:
            raise RuntimeError("Simulated place failure: arm jammed")
        await super().place(location)


@dataclass
class Bench:
    runtime: SystemRuntime
    workflow: WorkflowTemplate
    arm: _ArmThatJams
    device: _ShakerThatRemembers
    reservations: LocationReservationManager
    system: ISystem


async def _build_bench(*, end_pads: list[str] | None = None) -> Bench:
    """One shaker, three pads the arm reaches and one it does not.

    The plate starts at pad1, shakes at shaker1, and ends at one of
    ``end_pads``, so a test can jam either the move onto the device or the move
    home and get an operator's hands into the run at that point.
    """
    device = _ShakerThatRemembers("shaker1")
    arm = _ArmThatJams("robot1", ["shaker1", *PADS])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(arm)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"shaker1": device}, pads=[*PADS, STRANDED],
    )

    @orca.action(device=pool, inputs=[plate])
    async def shake(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    async def _method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake

    method = MethodTemplate("shake_method", func=_method)

    async def _thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield method

    ends: list[Location | str] = [
        system_map.get_location(p) for p in (end_pads or ["pad1"])
    ]
    thread = ThreadTemplate(
        labware_template=plate,
        start=system_map.get_location("pad1"),
        end=ends if len(ends) > 1 else ends[0],
        func=_thread,
    )
    workflow = WorkflowTemplate("hands_on_workflow")
    workflow.add_thread(thread, is_start=True)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
    )
    await builder.bind_labwares()
    system = builder.get_system()
    return Bench(
        runtime=SystemRuntime(system, event_bus=event_bus),
        workflow=workflow,
        arm=arm,
        device=device,
        reservations=builder._thread_reservation_coordinator._reservation_manager,
        system=system,
    )


async def _plate_lands_at(bench: Bench, position_id: str) -> LabwareSnapshot:
    """Put a plate on a position without going through an operator verb.

    Every verb that states a position refuses one another thread has reserved,
    which is what ``TestWhatTheOperatorIsAndIsNotRefused`` is about. A test that
    needs a position both taken AND spoken for cannot get there through them,
    so it writes the holders directly the way an arriving move does.
    """
    snapshot = await bench.runtime.labware.register("plate_96", confirm=True)
    instance = next(
        lw for lw in bench.system.labwares if lw.id == snapshot.id
    )
    await bench.system.labware_placer.place(
        instance, bench.system.system_map.get_location(position_id),
    )
    return snapshot


async def _abandon(bench: Bench, execution_id: str, thread_id: str) -> None:
    with contextlib.suppress(Exception):
        bench.runtime.recover_thread(
            execution_id, thread_id, RecoveryDecision.ABORT_THREAD,
        )
    with contextlib.suppress(Exception):
        await asyncio.wait_for(bench.runtime.wait(execution_id), timeout=20.0)


class TestTheMoveIsPlannedAgainFromWhereTheLabwareNowIs:

    async def test_a_relocated_plate_is_collected_from_where_the_operator_put_it(
        self,
    ) -> None:
        """The whole feature.

        The arm jams mid-place, so the plate is in the jaws and the run stops.
        The operator lifts it out and puts it on a free pad, records that, and
        says RETRY. The move that was planned from pad1 is thrown away and a
        new one collects the plate from pad3 and finishes at the same device.
        """
        bench = await _build_bench()
        bench.arm.jam_at = SHAKER_SITE
        await bench.runtime.start()
        try:
            record = await bench.runtime.submit_workflow(
                bench.workflow.name, mode=WorkflowRunMode.PURE_SIM,
            )
            paused = await wait_for_paused_thread(bench.runtime, record.id)
            assert paused.labware_id is not None
            assert bench.arm.pick_sources == ["pad1"]

            await bench.runtime.labware.edit_location(
                paused.labware_id, "pad3", confirm=True,
                reason="Lifted it out of the jaws onto pad3 by hand.",
            )
            bench.arm.jam_at = None
            bench.runtime.recover_thread(
                record.id, paused.id, RecoveryDecision.RETRY,
            )

            status = await asyncio.wait_for(
                bench.runtime.wait(record.id), timeout=30.0,
            )
            assert status.status == ExecutionState.COMPLETED
            assert "pad3" in bench.arm.pick_sources, (
                "the replanned move must collect the plate from where the "
                f"operator put it; picks were {bench.arm.pick_sources}"
            )
            assert bench.device.shakes, (
                "the action the move was feeding never ran"
            )
        finally:
            await bench.runtime.shutdown()

    async def test_a_thread_waiting_on_a_reservation_replans_without_being_retried(
        self,
    ) -> None:
        """Nobody has to pause the thread for it to hear the correction.

        The move home is blocked on a pad another plate is standing on, so the
        thread is queueing for a reservation with hours of waiting ahead of it.
        The operator moves the plate off the shaker and says so; the pending
        request is withdrawn and re-made from the new position, with no
        recovery decision anywhere in the sequence.
        """
        bench = await _build_bench(end_pads=["pad2"])
        await bench.runtime.start()
        try:
            blocker = await bench.runtime.labware.register(
                "plate_96", location="pad2", confirm=True,
            )
            record = await bench.runtime.submit_workflow(
                bench.workflow.name, mode=WorkflowRunMode.PURE_SIM,
            )
            await wait_until(
                lambda: bool(bench.device.shakes) and any(
                    t.status == "AWAITING_MOVE_RESERVATION"
                    for t in bench.runtime.list_threads(record.id)
                ),
                timeout=20.0,
                message="the move home never blocked on the occupied pad",
            )
            plate = bench.runtime.list_threads(record.id)[0]
            assert plate.labware_id is not None
            assert bench.arm.pick_sources == ["pad1"]

            await bench.runtime.labware.edit_location(
                plate.labware_id, "pad3", confirm=True,
                reason="Took it off the shaker and parked it on pad3.",
            )
            await bench.runtime.labware.discharge_labware(blocker.id)

            status = await asyncio.wait_for(
                bench.runtime.wait(record.id), timeout=30.0,
            )
            assert status.status == ExecutionState.COMPLETED
            assert bench.arm.pick_sources == ["pad1", "pad3"], (
                "the queued move must be re-made from the new position, and "
                "the shaker never picked from again"
            )
        finally:
            await bench.runtime.shutdown()

    async def test_a_plate_carried_to_the_target_needs_no_second_trip(
        self,
    ) -> None:
        """The path that already worked and must keep working.

        The operator finished the move themselves and said RETRY rather than
        CONTINUE. Replanning must not turn that into a route from the target to
        itself: there is nothing left to carry, and the arm is not sent at a
        position that already holds the plate.
        """
        bench = await _build_bench()
        bench.arm.jam_at = SHAKER_SITE
        await bench.runtime.start()
        try:
            record = await bench.runtime.submit_workflow(
                bench.workflow.name, mode=WorkflowRunMode.PURE_SIM,
            )
            paused = await wait_for_paused_thread(bench.runtime, record.id)
            assert paused.labware_id is not None

            await bench.runtime.labware.edit_location(
                paused.labware_id, "shaker1", confirm=True,
                reason="Put it on the shaker myself.",
            )
            bench.arm.jam_at = None
            bench.runtime.recover_thread(
                record.id, paused.id, RecoveryDecision.RETRY,
            )

            status = await asyncio.wait_for(
                bench.runtime.wait(record.id), timeout=30.0,
            )
            assert status.status == ExecutionState.COMPLETED
            assert bench.arm.place_targets.count(SHAKER_SITE) == 1, (
                "the arm was sent at a target the operator had already filled"
            )
            assert bench.device.shakes
        finally:
            await bench.runtime.shutdown()

    async def test_restating_a_position_the_plate_already_has_disturbs_nothing(
        self,
    ) -> None:
        """Confirming where a plate is is not moving it.

        An operator reading the ledger back to the system, or a client
        retrying a call, must not cost the thread the move it already holds:
        the plate has not moved, so the plan is not stale and the arm carries
        on with the plate it is already holding rather than picking again.
        """
        bench = await _build_bench()
        bench.arm.jam_at = SHAKER_SITE
        await bench.runtime.start()
        try:
            record = await bench.runtime.submit_workflow(
                bench.workflow.name, mode=WorkflowRunMode.PURE_SIM,
            )
            paused = await wait_for_paused_thread(bench.runtime, record.id)
            assert paused.labware_id is not None
            here = (
                await bench.runtime.labware.get_by_id(paused.labware_id)
            ).current_location
            assert here is not None
            held = bench.reservations.get_reservation_at(SHAKER_SITE)
            assert held is not None

            await bench.runtime.labware.edit_location(
                paused.labware_id, here, confirm=True,
                reason="Confirming the ledger is right.",
            )

            assert bench.reservations.get_reservation_at(SHAKER_SITE) is held, (
                "a restatement must not cost the thread its granted position"
            )
            bench.arm.jam_at = None
            bench.runtime.recover_thread(
                record.id, paused.id, RecoveryDecision.RETRY,
            )
            status = await asyncio.wait_for(
                bench.runtime.wait(record.id), timeout=30.0,
            )
            assert status.status == ExecutionState.COMPLETED
            assert bench.arm.pick_sources == ["pad1", SHAKER_SITE], (
                "the arm already held the plate, so the retry must place it "
                "rather than collect it again; the only other pick is the "
                f"move home (picks were {bench.arm.pick_sources})"
            )
        finally:
            await bench.runtime.shutdown()

    async def test_a_position_nothing_can_reach_pauses_instead_of_killing_the_run(
        self,
    ) -> None:
        """An operator can be wrong twice and still fix it.

        Name a spot no arm serves and the new plan has nowhere to start from.
        That is another thing for a person to correct, so the thread stops and
        waits for them rather than failing outright and taking every recovery
        verb with it.
        """
        bench = await _build_bench()
        bench.arm.jam_at = SHAKER_SITE
        await bench.runtime.start()
        try:
            record = await bench.runtime.submit_workflow(
                bench.workflow.name, mode=WorkflowRunMode.PURE_SIM,
            )
            paused = await wait_for_paused_thread(bench.runtime, record.id)
            assert paused.labware_id is not None

            await bench.runtime.labware.edit_location(
                paused.labware_id, STRANDED, confirm=True,
                reason="Set it down on the bench by the door.",
            )
            bench.arm.jam_at = None
            bench.runtime.recover_thread(
                record.id, paused.id, RecoveryDecision.RETRY,
            )

            await wait_until(
                lambda: any(
                    t.status == "PAUSED"
                    and "re-plan" in (t.pause_message or "")
                    for t in bench.runtime.list_threads(record.id)
                ),
                timeout=20.0,
                message="an unreachable position did not pause the thread",
            )
            assert bench.runtime.get_execution(record.id).status != (
                ExecutionState.FAILED
            ), "the run must still be recoverable"

            await bench.runtime.labware.edit_location(
                paused.labware_id, "pad3", confirm=True,
                reason="Moved it back onto pad3, which the arm reaches.",
            )
            bench.runtime.recover_thread(
                record.id, paused.id, RecoveryDecision.RETRY,
            )
            status = await asyncio.wait_for(
                bench.runtime.wait(record.id), timeout=30.0,
            )
            assert status.status == ExecutionState.COMPLETED
            assert "pad3" in bench.arm.pick_sources
        finally:
            await bench.runtime.shutdown()

    async def test_a_plate_carried_to_another_end_shelf_finishes_the_move(
        self,
    ) -> None:
        """A move to a hotel's shelves is done when the plate is on any of
        them. The operator picked a different shelf from the one the plan
        chose, which is an arrival, not a reason to route from a shelf to
        itself."""
        bench = await _build_bench(end_pads=["pad2", "pad3"])
        bench.arm.jam_at = "pad2"
        await bench.runtime.start()
        try:
            record = await bench.runtime.submit_workflow(
                bench.workflow.name, mode=WorkflowRunMode.PURE_SIM,
            )
            paused = await wait_for_paused_thread(bench.runtime, record.id)
            assert paused.labware_id is not None

            await bench.runtime.labware.edit_location(
                paused.labware_id, "pad3", confirm=True,
                reason="Put it on the other shelf.",
            )
            bench.runtime.recover_thread(
                record.id, paused.id, RecoveryDecision.RETRY,
            )

            status = await asyncio.wait_for(
                bench.runtime.wait(record.id), timeout=30.0,
            )
            assert status.status == ExecutionState.COMPLETED
            assert bench.arm.place_targets.count("pad3") == 0, (
                "the plate was already on pad3; the arm must not have gone"
            )
            assert bench.reservations.get_reservation_at("pad2") is None, (
                "the abandoned shelf must not stay claimed"
            )
        finally:
            await bench.runtime.shutdown()

    async def test_the_stale_plan_gives_up_the_position_it_had_reserved(
        self,
    ) -> None:
        """A move home owns the reservation on its destination. Once the plan
        is thrown away that claim is released, so the position is free for
        whoever else wants it rather than held by a route nobody is taking."""
        bench = await _build_bench(end_pads=["pad2"])
        bench.arm.jam_at = "pad2"
        await bench.runtime.start()
        try:
            record = await bench.runtime.submit_workflow(
                bench.workflow.name, mode=WorkflowRunMode.PURE_SIM,
            )
            paused = await wait_for_paused_thread(bench.runtime, record.id)
            assert paused.labware_id is not None
            assert bench.reservations.get_reservation_at("pad2") is not None, (
                "the move home should be holding its destination"
            )

            # Something else takes pad2, so the replanned move cannot simply
            # re-reserve it and the release stays observable.
            await _plate_lands_at(bench, "pad2")
            await bench.runtime.labware.edit_location(
                paused.labware_id, "pad3", confirm=True,
                reason="Took it out of the jaws and parked it on pad3.",
            )
            bench.runtime.recover_thread(
                record.id, paused.id, RecoveryDecision.RETRY,
            )

            await wait_until(
                lambda: bench.reservations.get_reservation_at("pad2") is None,
                timeout=15.0,
                message="the thrown-away plan never released pad2",
            )
        finally:
            await bench.runtime.shutdown()


class TestWhatTheOperatorIsAndIsNotRefused:

    async def test_owning_the_labware_is_not_a_reason_to_refuse(self) -> None:
        """A live thread holding the plate must never block the operator from
        saying where the plate really is; correcting the books under a running
        thread is the whole point of the verb."""
        bench = await _build_bench()
        bench.arm.jam_at = SHAKER_SITE
        await bench.runtime.start()
        try:
            record = await bench.runtime.submit_workflow(
                bench.workflow.name, mode=WorkflowRunMode.PURE_SIM,
            )
            paused = await wait_for_paused_thread(bench.runtime, record.id)
            assert paused.labware_id is not None

            await bench.runtime.labware.edit_location(
                paused.labware_id, "pad3", confirm=True,
                reason="It is on pad3.",
            )

            snapshot = await bench.runtime.labware.get_by_id(paused.labware_id)
            assert snapshot.current_location == "pad3"
            await _abandon(bench, record.id, paused.id)
        finally:
            await bench.runtime.shutdown()

    async def test_a_target_another_plate_is_standing_on_is_refused(
        self,
    ) -> None:
        bench = await _build_bench()
        bench.arm.jam_at = SHAKER_SITE
        await bench.runtime.start()
        try:
            record = await bench.runtime.submit_workflow(
                bench.workflow.name, mode=WorkflowRunMode.PURE_SIM,
            )
            paused = await wait_for_paused_thread(bench.runtime, record.id)
            assert paused.labware_id is not None
            resident = await bench.runtime.labware.register(
                "plate_96", location="pad3", confirm=True,
            )

            with pytest.raises(SlotOccupiedError) as exc:
                await bench.runtime.labware.edit_location(
                    paused.labware_id, "pad3", confirm=True,
                    reason="It is on pad3.",
                )

            assert exc.value.position_id == "pad3"
            assert exc.value.existing_labware_name == resident.name
            resident_now = await bench.runtime.labware.get_by_id(resident.id)
            assert resident_now.current_location == "pad3", (
                "a refused edit must leave the resident where it was"
            )
            await _abandon(bench, record.id, paused.id)
        finally:
            await bench.runtime.shutdown()

    async def test_a_target_another_thread_has_reserved_is_refused(
        self,
    ) -> None:
        """A reservation is a claim on somewhere to put a plate down, made
        before the arm leaves, so the site can be spoken for while it still
        looks empty. Writing another plate onto it would strand that move."""
        bench = await _build_bench()
        bench.arm.jam_at = SHAKER_SITE
        await bench.runtime.start()
        try:
            record = await bench.runtime.submit_workflow(
                bench.workflow.name, mode=WorkflowRunMode.PURE_SIM,
            )
            paused = await wait_for_paused_thread(bench.runtime, record.id)
            stranger = await bench.runtime.labware.register(
                "plate_96", location="pad3", confirm=True,
            )
            held = bench.reservations.get_reservation_at(SHAKER_SITE)
            assert held is not None

            with pytest.raises(LocationReservedError) as exc:
                await bench.runtime.labware.edit_location(
                    stranger.id, "shaker1", confirm=True,
                    reason="Putting this one on the shaker.",
                )

            assert exc.value.position_id == SHAKER_SITE
            assert exc.value.holder_thread_id == paused.id
            assert exc.value.reservation_id == held.id, (
                "name the claim itself, so the operator can cancel it"
            )
            assert paused.name in str(exc.value), (
                "name the thread holding the position, so the operator knows "
                "what to clear"
            )
            stranger_now = await bench.runtime.labware.get_by_id(stranger.id)
            assert stranger_now.current_location == "pad3", (
                "a refused edit must not have moved anything"
            )
            await _abandon(bench, record.id, paused.id)
        finally:
            await bench.runtime.shutdown()

    async def test_a_plate_in_the_way_is_named_ahead_of_a_reservation(
        self,
    ) -> None:
        """A position can be both taken and spoken for. The plate standing
        there is what the operator can act on, so that is the refusal they
        get: the reservation only matters once the position is clear."""
        bench = await _build_bench(end_pads=["pad2"])
        bench.arm.jam_at = "pad2"
        await bench.runtime.start()
        try:
            record = await bench.runtime.submit_workflow(
                bench.workflow.name, mode=WorkflowRunMode.PURE_SIM,
            )
            paused = await wait_for_paused_thread(bench.runtime, record.id)
            assert bench.reservations.get_reservation_at("pad2") is not None
            resident = await _plate_lands_at(bench, "pad2")
            stranger = await bench.runtime.labware.register(
                "plate_96", location="pad3", confirm=True,
            )

            with pytest.raises(SlotOccupiedError) as exc:
                await bench.runtime.labware.edit_location(
                    stranger.id, "pad2", confirm=True,
                    reason="Trying to put this one on pad2.",
                )

            assert exc.value.existing_labware_name == resident.name
            await _abandon(bench, record.id, paused.id)
        finally:
            await bench.runtime.shutdown()

    async def test_the_reserving_thread_may_have_its_own_plate_put_there(
        self,
    ) -> None:
        """The reservation belongs to the thread carrying this very plate, so
        the operator putting it there is that thread's move being finished by
        hand, not a competing claim."""
        bench = await _build_bench()
        bench.arm.jam_at = SHAKER_SITE
        await bench.runtime.start()
        try:
            record = await bench.runtime.submit_workflow(
                bench.workflow.name, mode=WorkflowRunMode.PURE_SIM,
            )
            paused = await wait_for_paused_thread(bench.runtime, record.id)
            assert paused.labware_id is not None
            assert bench.reservations.get_reservation_at(SHAKER_SITE) is not None

            await bench.runtime.labware.edit_location(
                paused.labware_id, "shaker1", confirm=True,
                reason="Put it on the shaker myself.",
            )

            snapshot = await bench.runtime.labware.get_by_id(paused.labware_id)
            assert snapshot.current_location == SHAKER_SITE
            await _abandon(bench, record.id, paused.id)
        finally:
            await bench.runtime.shutdown()

    async def test_register_is_guarded_the_same_way(self) -> None:
        """The third verb that states a position. It was the one that did not
        ask who had claimed the slot, which is how a plate landed on a pad a
        move was already on its way to."""
        bench = await _build_bench()
        bench.arm.jam_at = SHAKER_SITE
        await bench.runtime.start()
        try:
            record = await bench.runtime.submit_workflow(
                bench.workflow.name, mode=WorkflowRunMode.PURE_SIM,
            )
            paused = await wait_for_paused_thread(bench.runtime, record.id)
            held = bench.reservations.get_reservation_at(SHAKER_SITE)
            assert held is not None

            with pytest.raises(LocationReservedError) as exc:
                await bench.runtime.labware.register(
                    "plate_96", location="shaker1", confirm=True,
                )

            assert exc.value.position_id == SHAKER_SITE
            assert exc.value.holder_thread_id == paused.id
            assert paused.name in str(exc.value)
            assert not [
                lw for lw in await bench.runtime.labware.list_all()
                if lw.current_location == SHAKER_SITE
            ], "a refused register must not have left an instance behind"
            await _abandon(bench, record.id, paused.id)
        finally:
            await bench.runtime.shutdown()

    async def test_reset_location_is_guarded_and_replans_the_same_way(
        self,
    ) -> None:
        """The sibling verb writes the same holders and moves the same plate,
        so it cannot be the way round a guard: it refuses a reserved position
        too, and a thread carrying the plate plans again from where it lands.
        Wiping the history is the only part that stays its own."""
        bench = await _build_bench()
        bench.arm.jam_at = SHAKER_SITE
        await bench.runtime.start()
        try:
            record = await bench.runtime.submit_workflow(
                bench.workflow.name, mode=WorkflowRunMode.PURE_SIM,
            )
            paused = await wait_for_paused_thread(bench.runtime, record.id)
            assert paused.labware_id is not None
            stranger = await bench.runtime.labware.register(
                "plate_96", location="pad3", confirm=True,
            )

            with pytest.raises(LocationReservedError):
                await bench.runtime.labware.reset_location(
                    stranger.id, "shaker1", confirm=True,
                    reason="Putting this one on the shaker.",
                )

            await bench.runtime.labware.reset_location(
                paused.labware_id, "pad2", confirm=True,
                reason="Starting this plate's history over on pad2.",
            )
            bench.arm.jam_at = None
            bench.runtime.recover_thread(
                record.id, paused.id, RecoveryDecision.RETRY,
            )

            status = await asyncio.wait_for(
                bench.runtime.wait(record.id), timeout=30.0,
            )
            assert status.status == ExecutionState.COMPLETED
            assert "pad2" in bench.arm.pick_sources, (
                "reset-location must send the thread replanning like its "
                f"sibling does; picks were {bench.arm.pick_sources}"
            )
        finally:
            await bench.runtime.shutdown()
