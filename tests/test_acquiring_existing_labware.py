"""How a run takes hold of labware that already exists: every row of the rule, then real runs.

The rule (orca.workflow_models.acquisition):
- At submit, a labware named by barcode is refused when its thread takes new labware from a
  stacker, it is the wrong type, named twice, in use by a running execution, in an arm's jaws, or a
  tip rack whose tips nobody stated. Each refusal says what to do. A start holding other labware is
  refused by the start-location check, the same as for every other thread, unless the same run
  named that labware and its thread carries it off.
- At thread start: not on the deck, it is placed at the start; at the start, it is used where it
  is; elsewhere and reachable, the arm carries it; elsewhere and unreachable, a person places it
  (in sim the engine plays that person, and records it as a placement, never a carry).
"""
import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

import orca.orca as orca
from orca.config import OrcaConfig
import orca.workflow_models.spawn_actions as spawn_actions
from orca.resource_models.device_error import DeviceBusyError
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_location_service import InMemoryLabwareLocationService
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resource_pool import ResourcePool
from orca.workflow_models.labware_group import (
    AcquisitionValidationError,
    BarcodeAcquisition,
    LabwareGroup,
    LabwareGroupMember,
)
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.errors import StartLocationsOccupiedError
from orca.workflow_models.submission_modes import BatchMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, WorkflowTemplate
from orca.spawn import LEAVE_IN_PLACE
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.acquisition import MemberFacts, StartDecision, StartFacts, decide_start, refusal_for
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.method_template import ActionTemplate
from orca.workflow_models.spawn_actions import RelocateByHandSpawn
from orca.workflow_models.thread_context import ThreadContext
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from tests.placements_for_tests import this_tests_placements
from tests.test_helpers import (
    make_labware_placer,
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    execution_outcome,
    wait_until,
    wire_system_map,
)


@pytest.mark.parametrize(("facts", "decision"), [
    (StartFacts(present=False, at_start=False, reachable=False), StartDecision.SPAWN),
    (StartFacts(present=True, at_start=True, reachable=True), StartDecision.USE_IN_PLACE),
    (StartFacts(present=True, at_start=False, reachable=True), StartDecision.MOVE),
    (StartFacts(present=True, at_start=False, reachable=False), StartDecision.ASK_OPERATOR),
])
def test_where_the_labware_is_decides_how_the_thread_gets_it(facts: StartFacts, decision: StartDecision) -> None:
    assert decide_start(facts) is decision


_OK = MemberFacts(labware_name="DNA-1", labware_type="plate_96", wanted_type="plate_96", thread_template="sample", start="pad1")


@pytest.mark.parametrize(("facts", "says"), [
    (replace(_OK, labware_type="trough"), "needs a plate_96"),
    (replace(_OK, also_named_by="group g2"), "also named by group g2"),
    (replace(_OK, held_by="thread plate-7"), "in use by thread plate-7"),
    (replace(_OK, in_jaws_of="robot1/gripper"), "in robot1/gripper"),
    (replace(_OK, tip_layout_unknown=True), "set-tip-state"),
    (replace(_OK, start_dispenses=True), "takes new labware from pad1"),
])
def test_each_refusal_says_what_to_do(facts: MemberFacts, says: str) -> None:
    refusal = refusal_for(facts)
    assert refusal is not None and says in refusal


def test_a_usable_labware_is_not_refused() -> None:
    assert refusal_for(_OK) is None


async def _system(*, pad2_reachable: bool, left_on_start: bool = False) -> tuple[ISystem, WorkflowTemplate]:
    device = create_test_device("shaker1")
    reach = ["shaker1", "pad1", "pad2"] if pad2_reachable else ["shaker1", "pad1"]
    transporter = create_test_transporter("robot1", reach)
    plate = create_test_plate_template("plate_96")
    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1", "pad2"])

    @orca.action(device=pool, inputs=[plate])
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shake_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_action

    pad1 = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad1, end=(pad1, LEAVE_IN_PLACE) if left_on_start else pad1)
    async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield shake_method

    workflow = WorkflowTemplate("simple_workflow")
    workflow.add_thread(plate_thread, is_start=True)
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[workflow], event_bus=EventBus(), config=OrcaConfig(),
        ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(), unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    return builder.get_system(), workflow


async def _runtime_with_plate_on_pad2(
    *, pad2_reachable: bool, left_on_start: bool = False,
) -> tuple[SystemRuntime, WorkflowTemplate, str]:
    system, workflow = await _system(pad2_reachable=pad2_reachable, left_on_start=left_on_start)
    runtime = await memory_runtime(system)
    await runtime.start()
    plate = await runtime.labware.register("plate_96", location="pad2", barcode="DNA-1", confirm=True)
    return runtime, workflow, plate.id


def _by_barcode(*barcodes: str) -> list[LabwareGroup]:
    return [
        LabwareGroup(id=f"g{i}", members=(LabwareGroupMember(
            thread_template_name="plate_thread", acquisition=BarcodeAcquisition(barcode=barcode),
        ),))
        for i, barcode in enumerate(barcodes, start=1)
    ]


async def _positions(runtime: SystemRuntime, labware_id: str) -> list[str]:
    return [entry.position_id for entry in await runtime.labware.get_history(labware_id)]


async def test_a_reachable_plate_is_carried_to_the_start_by_the_arm() -> None:
    runtime, workflow, plate_id = await _runtime_with_plate_on_pad2(pad2_reachable=True)
    try:
        submission = await runtime.submit(workflow, groups=_by_barcode("DNA-1"), mode=WorkflowRunMode.CHECK_RUN)
        assert (await execution_outcome(runtime, submission, timeout=30.0)).status == "completed"
        assert (await _positions(runtime, plate_id))[:3] == ["pad2", "robot1/gripper", "pad1"]
    finally:
        await runtime.shutdown()


async def test_an_unreachable_plate_is_placed_at_the_start_never_carried() -> None:
    runtime, workflow, plate_id = await _runtime_with_plate_on_pad2(pad2_reachable=False)
    try:
        submission = await runtime.submit(workflow, groups=_by_barcode("DNA-1"), mode=WorkflowRunMode.CHECK_RUN)
        assert (await execution_outcome(runtime, submission, timeout=30.0)).status == "completed"
        assert (await _positions(runtime, plate_id))[:2] == ["pad2", "pad1"]
    finally:
        await runtime.shutdown()


async def test_a_start_held_by_a_plate_of_the_same_submission_is_not_refused() -> None:
    runtime, workflow, waiting_plate_id = await _runtime_with_plate_on_pad2(pad2_reachable=True)
    try:
        await runtime.labware.register("plate_96", location="pad1", barcode="DNA-0", confirm=True)
        submission = await runtime.submit(workflow, groups=_by_barcode("DNA-0", "DNA-1"), mode=WorkflowRunMode.SIM)
        assert (await execution_outcome(runtime, submission, timeout=30.0)).status == "completed"
        assert (await _positions(runtime, waiting_plate_id))[:3] == ["pad2", "robot1/gripper", "pad1"]
    finally:
        await runtime.shutdown()


async def test_a_start_held_by_a_plate_of_the_joined_run_is_not_refused() -> None:
    runtime, workflow, joining_plate_id = await _runtime_with_plate_on_pad2(pad2_reachable=True)
    try:
        await runtime.labware.register("plate_96", location="pad1", barcode="DNA-0", confirm=True)
        first = await runtime.submit(
            workflow, groups=_by_barcode("DNA-0"), batch_mode=BatchMode.JOIN_EXISTING, mode=WorkflowRunMode.SIM,
        )
        await asyncio.wait_for(runtime._executions[first.execution_id].workflow_attached.wait(), timeout=10.0)
        second = await runtime.submit(
            workflow, groups=[replace(_by_barcode("DNA-1")[0], id="g2")],
            batch_mode=BatchMode.JOIN_EXISTING, mode=WorkflowRunMode.SIM,
        )
        assert second.execution_id == first.execution_id
        assert (await execution_outcome(runtime, first, timeout=30.0)).status == "completed"
        assert (await _positions(runtime, joining_plate_id))[:3] == ["pad2", "robot1/gripper", "pad1"]
    finally:
        await runtime.shutdown()


async def test_a_plate_that_will_be_left_on_the_start_still_blocks_it() -> None:
    runtime, workflow, _ = await _runtime_with_plate_on_pad2(pad2_reachable=True, left_on_start=True)
    try:
        await runtime.labware.register("plate_96", location="pad1", barcode="DNA-0", confirm=True)
        with pytest.raises(StartLocationsOccupiedError, match="pad1"):
            await runtime.submit(workflow, groups=_by_barcode("DNA-0", "DNA-1"), mode=WorkflowRunMode.SIM)
    finally:
        await runtime.shutdown()


async def test_a_start_held_by_a_plate_the_submission_does_not_name_is_refused() -> None:
    runtime, workflow, _ = await _runtime_with_plate_on_pad2(pad2_reachable=True)
    try:
        await runtime.labware.register("plate_96", location="pad1", barcode="BYSTANDER", confirm=True)
        with pytest.raises(StartLocationsOccupiedError, match="pad1"):
            await runtime.submit(workflow, groups=_by_barcode("DNA-1"), mode=WorkflowRunMode.SIM)
    finally:
        await runtime.shutdown()


async def test_one_plate_named_by_two_groups_is_refused_at_submit() -> None:
    runtime, workflow, _ = await _runtime_with_plate_on_pad2(pad2_reachable=True)
    try:
        with pytest.raises(AcquisitionValidationError, match="also named by"):
            await runtime.submit(workflow, groups=_by_barcode("DNA-1", "DNA-1"), mode=WorkflowRunMode.CHECK_RUN)
    finally:
        await runtime.shutdown()


async def test_a_plate_another_run_is_using_is_refused_at_submit() -> None:
    runtime, workflow, _ = await _runtime_with_plate_on_pad2(pad2_reachable=True)
    try:
        first = await runtime.submit(workflow, groups=_by_barcode("DNA-1"), mode=WorkflowRunMode.CHECK_RUN)
        runtime.pause_execution(first.execution_id)
        with pytest.raises(AcquisitionValidationError, match="in use by"):
            await runtime.submit(workflow, groups=_by_barcode("DNA-1"), mode=WorkflowRunMode.CHECK_RUN)
    finally:
        await runtime.shutdown()


async def test_in_live_a_person_moving_it_ends_the_wait_and_a_new_plate_does_not() -> None:
    """The wait ends when this labware is recorded at the start (edit-location), never on some
    other labware appearing there."""
    start = Location("pad1", resource=PlatePad("pad1"))
    elsewhere = Location("pad2", resource=PlatePad("pad2"))
    plate = LabwareInstance("plate_96", "_sim_plate")
    service = InMemoryLabwareLocationService(this_tests_placements())
    elsewhere.initialize_labware(plate)
    service.update(plate, elsewhere)
    thread = LabwareThreadInstance(labware=plate, start_location=start, end_locations=[start], run_mode=WorkflowRunMode.LIVE)
    spawn = RelocateByHandSpawn(start, elsewhere, service, make_labware_placer(service))
    waiting = asyncio.create_task(spawn.acquire(thread))

    stranger = LabwareInstance("plate_96", "_sim_plate")
    service.update(stranger, start)
    await asyncio.sleep(0)
    assert not spawn._arrived(thread)
    assert not waiting.done()

    service.update(plate, start)
    await asyncio.wait_for(waiting, timeout=5.0)


@pytest.mark.asyncio
async def test_a_simulated_move_by_hand_waits_out_a_busy_start(monkeypatch: pytest.MonkeyPatch) -> None:
    start = Location("pad1", resource=PlatePad("pad1"))
    elsewhere = Location("pad2", resource=PlatePad("pad2"))
    plate = LabwareInstance("plate_96", "_sim_plate")
    thread = LabwareThreadInstance(labware=plate, start_location=start, end_locations=[start], run_mode=WorkflowRunMode.CHECK_RUN)
    placer = MagicMock()
    placer.relocate = AsyncMock(side_effect=[DeviceBusyError("busy"), None])
    spawn = RelocateByHandSpawn(start, elsewhere, InMemoryLabwareLocationService(this_tests_placements()), placer)
    monkeypatch.setattr(spawn_actions, "_RETRY_BACKOFF_S", 0)

    await asyncio.wait_for(spawn.acquire(thread), timeout=5.0)

    assert placer.relocate.await_count == 2



def _pool_member_group(group_id: str) -> LabwareGroup:
    return LabwareGroup(id=group_id, members=(LabwareGroupMember(thread_template_name="plate_thread"),))


async def _threads_holding(runtime: SystemRuntime, execution_id: str, labware_id: str) -> int:
    await wait_until(lambda: len(runtime.list_threads(execution_id)) == 2, timeout=10.0)
    return [t.labware_id for t in runtime.list_threads(execution_id)].count(labware_id)


async def test_a_plate_named_by_barcode_is_not_also_given_to_a_pool_member_of_the_same_submission() -> None:
    runtime, workflow, _ = await _runtime_with_plate_on_pad2(pad2_reachable=True)
    try:
        named = await runtime.labware.register("plate_96", location="pad1", barcode="DNA-0", confirm=True)
        groups = [*_by_barcode("DNA-0"), _pool_member_group("g2")]
        submission = await runtime.submit(workflow, groups=groups, mode=WorkflowRunMode.SIM)
        assert await _threads_holding(runtime, submission.execution_id, named.id) == 1
    finally:
        await runtime.shutdown()


async def test_a_plate_the_joined_run_names_is_not_given_to_a_joining_pool_member() -> None:
    runtime, workflow, _ = await _runtime_with_plate_on_pad2(pad2_reachable=True)
    try:
        named = await runtime.labware.register("plate_96", location="pad1", barcode="DNA-0", confirm=True)
        first = await runtime.submit(
            workflow, groups=_by_barcode("DNA-0"), batch_mode=BatchMode.JOIN_EXISTING, mode=WorkflowRunMode.SIM,
        )
        await asyncio.wait_for(runtime._executions[first.execution_id].workflow_attached.wait(), timeout=10.0)
        second = await runtime.submit(
            workflow, groups=[_pool_member_group("g2")], batch_mode=BatchMode.JOIN_EXISTING, mode=WorkflowRunMode.SIM,
        )
        assert second.execution_id == first.execution_id
        assert await _threads_holding(runtime, first.execution_id, named.id) == 1
    finally:
        await runtime.shutdown()
