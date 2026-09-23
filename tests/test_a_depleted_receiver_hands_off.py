"""A receiver whose labware runs out must leave, even with slot room to spare.

Two gates decide whether a contribution binds to the active receiver: the
labware's ``can_continue()`` and the slot's ``has_room()``. Both must pass.
The receiver's own exit test used to read only ``has_room()``, so a receiver
whose labware was spent while its slot still had room kept looping. The
contribution overflowed into ``slot.pending``, which only drains when the
receiver ends, and the receiver would not end. The stall detector broke the
tie on a real Flex + PF400.

For a tip rack that is the ordinary case: ``max_contributions`` is the rack's
column ceiling, and a rack usually runs dry well before that many columns are
drawn. The one campaign workflow that exercised overflow set
``max_contributions=1``, so every overflow it saw was slot-full -- the branch
that already worked.

These run an assembled ``SystemRuntime``. The seam that broke sits BETWEEN
the two predicates, so a mocked test of either one passes.
"""

from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.resource_models.capacity import CapacityPolicy, OverflowAction
from orca.resource_models.labware import LabwareInstance, PlateTemplate
from orca.resource_models.resource_pool import ResourcePool
from orca.resource_models.sharing import GroupSharing, SubmissionBatching
from orca.state.records import DeclaredTracking
from orca.runtime.labware_group import LabwareGroup, LabwareGroupMember
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.submission import BatchMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.spawn import LEAVE_IN_PLACE, REUSE_EXISTING
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import ActionTemplate, IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.thread_template import EndArg, StartArg

from orca.resource_models.labware_state import LabwareSlot

from tests.closure_e2e_scaffold import TERMINAL_STATUSES
from tests.test_helpers import (
    create_test_device,
    create_test_transporter,
    wait_until,
    wire_system_map,
)

_LABWARE_TYPE = "Cor_Falcon_96_wellplate_340ul_Fb_Black"


class _Depletion:
    """Stands in for a tip rack drawn down by the work it serves.

    ``can_continue`` answers False once a rack has served ``capacity`` draws.
    Rack identity is recorded so a test can tell a fresh rack from a re-bind
    of the spent one.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._draws: dict[str, int] = {}
        self.served_by: list[str] = []

    def draw(self, labware: LabwareInstance) -> None:
        self._draws[labware.id] = self._draws.get(labware.id, 0) + 1
        self.served_by.append(labware.id)

    async def can_continue(
        self, labware: LabwareInstance, demand: DeclaredTracking | None = None,
    ) -> bool:
        del demand
        return self._draws.get(labware.id, 0) < self._capacity


async def _build_system(
    depletion: _Depletion, *, deck_resident: bool = False,
) -> tuple[ISystem, WorkflowTemplate, EventBus]:
    """Two stations, a transporter, and a `sample -> rack` contribution chain.

    ``deck_resident`` declares the receiver's home pad REUSE_EXISTING /
    LEAVE_IN_PLACE, which is how a tip rack that lives on the deck across
    executions is written.
    """
    sample_station = create_test_device("sample_station", site_names=["site-1", "site-2"])
    rack_station = create_test_device("rack_station", site_names=["site-1", "site-2"])
    transporter = create_test_transporter(
        "robot1", ["sample_pad", "sample_station", "rack_pad", "rack_station", "waste"],
    )

    sample = PlateTemplate("sample", labware_type=_LABWARE_TYPE)
    rack = PlateTemplate(
        "rack",
        labware_type=_LABWARE_TYPE,
        can_continue_fn=depletion.can_continue,
        group_sharing=GroupSharing.SHARED_ACROSS_GROUPS,
        submission_batching=SubmissionBatching.BATCHABLE,
    )

    registry = ResourceRegistry()
    for resource in (sample_station, rack_station, transporter):
        registry.add_resource(resource)
    sample_station_pool = ResourcePool("sample_station", [sample_station])
    rack_station_pool = ResourcePool("rack_station", [rack_station])
    registry.add_resource_pool(sample_station_pool)
    registry.add_resource_pool(rack_station_pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map,
        devices={"sample_station": sample_station, "rack_station": rack_station},
        pads=["sample_pad", "rack_pad", "waste"],
    )

    @orca.action(device=rack_station_pool, inputs=[sample, rack])
    async def draw_from_rack(ctx: ActionContext) -> None:
        depletion.draw(ctx.labware("rack"))
        await ctx.device().shake(duration=1, speed=500)

    async def _draw_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield draw_from_rack
    draw_method = MethodTemplate("draw_method", func=_draw_method)

    async def _sample_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield draw_method
    sample_thread = ThreadTemplate(
        labware_template=sample,
        start=system_map.get_location("sample_pad"),
        end=system_map.get_location("waste"),
        func=_sample_thread,
        contributes_to=["rack"],
    )

    async def _rack_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        while ctx.has_more_work():
            yield orca.join(allows=[draw_method])
    rack_pad = system_map.get_location("rack_pad")
    rack_start: StartArg = (rack_pad, REUSE_EXISTING) if deck_resident else rack_pad
    rack_end: EndArg = (rack_pad, LEAVE_IN_PLACE) if deck_resident else rack_pad
    rack_thread = ThreadTemplate(
        labware_template=rack,
        start=rack_start,
        end=rack_end,
        func=_rack_thread,
    )

    workflow = WorkflowTemplate("depleting_rack")
    workflow.add_thread(sample_thread, is_start=True)
    workflow.add_thread(rack_thread)
    # Room for four contributions; the rack runs dry after one. The gap
    # between those two numbers is the whole defect.
    workflow.register_auto_spawn(
        rack_thread,
        capacity=CapacityPolicy(max_contributions=4, overflow_action=OverflowAction.NEW),
    )

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="depleting_rack_system",
        description="",
        labwares=[sample, rack],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    return builder.get_system(), workflow, event_bus


def _rack_slot(runtime: SystemRuntime, execution_id: str) -> LabwareSlot | None:
    workflow = runtime._executions[execution_id].executing_workflow
    if workflow is None:
        return None
    registry = workflow._labware_registry
    assert registry is not None
    return next(
        (s for s in registry.all_slots().values()
         if s.labware_template_name == "rack"),
        None,
    )


def _sample_group(gid: str) -> LabwareGroup:
    return LabwareGroup(
        id=gid, members=(LabwareGroupMember(thread_template_name="sample"),),
    )


async def _run_two_contributions(
    depletion: _Depletion, *, deck_resident: bool = False,
) -> dict[str, str]:
    """Submit two samples at once and wait for the execution to quiesce."""
    system, workflow, event_bus = await _build_system(
        depletion, deck_resident=deck_resident,
    )
    runtime = SystemRuntime(system, event_bus=event_bus)
    await runtime.start()
    statuses: dict[str, str] = {}
    try:
        sub = await runtime.submit(
            workflow,
            groups=[_sample_group("grp-1"), _sample_group("grp-2")],
            batch_mode=BatchMode.STANDALONE,
            mode=WorkflowRunMode.PURE_SIM,
        )

        def _all_terminal() -> bool:
            nonlocal statuses
            statuses = {t.name: t.status for t in runtime.list_threads(sub.execution_id)}
            return bool(statuses) and all(s in TERMINAL_STATUSES for s in statuses.values())

        await wait_until(
            _all_terminal,
            timeout=60.0,
            message=(
                "the execution never quiesced: the depleted receiver kept "
                f"looping while its overflow sat undelivered; statuses={statuses}"
            ),
        )
    finally:
        await runtime.shutdown()
    return statuses


@pytest.mark.asyncio
async def test_a_depleted_receiver_leaves_so_its_overflow_is_delivered() -> None:
    depletion = _Depletion(capacity=1)
    statuses = await _run_two_contributions(depletion)

    assert len(depletion.served_by) == 2, (
        "both contributions must be served; the second one overflowed past a "
        f"receiver that never left, got {depletion.served_by}"
    )
    assert depletion.served_by[0] != depletion.served_by[1], (
        "the stashed contribution must go to a SUCCESSOR rack, not back to the "
        f"spent one, got {depletion.served_by}"
    )
    assert all(s == "COMPLETED" for s in statuses.values()), (
        f"every thread must COMPLETE cleanly; statuses={statuses}"
    )


@pytest.mark.asyncio
async def test_a_spent_deck_resident_rack_is_replaced_not_rebound() -> None:
    """A REUSE_EXISTING receiver adopts what stands on its deck site.

    Its replacement must not adopt the same spent rack. The spent one leaves
    by the thread's declared removal mechanism, and the replacement arrives by
    its declared spawn mechanism.
    """
    depletion = _Depletion(capacity=1)
    statuses = await _run_two_contributions(depletion, deck_resident=True)

    assert len(depletion.served_by) == 2, (
        f"both contributions must be served, got {depletion.served_by}"
    )
    assert depletion.served_by[0] != depletion.served_by[1], (
        "the replacement must be a FRESH rack; a re-bind of the spent one is "
        f"empty and overflows again, got {depletion.served_by}"
    )
    assert all(s == "COMPLETED" for s in statuses.values()), (
        f"every thread must COMPLETE cleanly; statuses={statuses}"
    )


@pytest.mark.asyncio
async def test_a_receiver_with_room_and_labware_left_keeps_looping() -> None:
    """The ordinary case must not regress: room left, labware fine, keep going."""
    depletion = _Depletion(capacity=10)
    statuses = await _run_two_contributions(depletion)

    assert len(depletion.served_by) == 2, (
        f"both contributions must be served, got {depletion.served_by}"
    )
    assert depletion.served_by[0] == depletion.served_by[1], (
        "one receiver with room to spare must serve both contributions, got "
        f"{depletion.served_by}"
    )
    assert all(s == "COMPLETED" for s in statuses.values()), (
        f"every thread must COMPLETE cleanly; statuses={statuses}"
    )


@pytest.mark.asyncio
async def test_a_depletion_landing_while_the_receiver_waits_still_hands_off() -> None:
    """The overflow can land while the receiver is already parked in its join.

    The exit test only runs at the top of the ``while ctx.has_more_work()``
    loop, so a receiver already blocked in ``orca.join()`` never re-reads it.
    Depletion has to wake that wait, not only fail the next test.
    """
    depletion = _Depletion(capacity=1)

    system, workflow, event_bus = await _build_system(depletion)
    runtime = SystemRuntime(system, event_bus=event_bus)
    await runtime.start()
    statuses: dict[str, str] = {}
    try:
        first = await runtime.submit(
            workflow,
            groups=[_sample_group("grp-1")],
            batch_mode=BatchMode.STANDALONE,
            mode=WorkflowRunMode.PURE_SIM,
        )
        # Wait for the receiver to be parked in its join again, which is the
        # state this test is about: it records itself in awaiting_threads on
        # an empty-handed entry.
        await wait_until(
            lambda: len(depletion.served_by) == 1,
            timeout=30.0,
            message="the first contribution was never served",
        )

        def _receiver_is_parked_in_its_join() -> bool:
            slot = _rack_slot(runtime, first.execution_id)
            return slot is not None and bool(slot.awaiting_threads)

        await wait_until(
            _receiver_is_parked_in_its_join,
            timeout=30.0,
            message="the receiver never settled back into orca.join()",
        )

        await runtime.submit(
            workflow,
            groups=[_sample_group("grp-2")],
            batch_mode=BatchMode.JOIN_EXISTING,
            mode=WorkflowRunMode.PURE_SIM,
        )

        def _all_terminal() -> bool:
            nonlocal statuses
            statuses = {t.name: t.status for t in runtime.list_threads(first.execution_id)}
            return bool(statuses) and all(s in TERMINAL_STATUSES for s in statuses.values())

        await wait_until(
            _all_terminal,
            timeout=60.0,
            message=(
                "a receiver already parked in its join never woke when its "
                f"labware ran out; statuses={statuses}"
            ),
        )
    finally:
        await runtime.shutdown()

    assert len(depletion.served_by) == 2, (
        f"both contributions must be served, got {depletion.served_by}"
    )
    assert depletion.served_by[0] != depletion.served_by[1], (
        f"the second contribution needs a fresh rack, got {depletion.served_by}"
    )
