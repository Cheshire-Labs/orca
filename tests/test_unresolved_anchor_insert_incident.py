"""An anchored insert still pending when the lane closes drops -- loudly.

The drop is expected behaviour (Before/After anchors routinely target a
conditional method/action that never runs, and a run can end with an insert
still queued). The contract is that the drop is surfaced as a WARNING
``UNRESOLVED_ANCHOR_INSERT`` incident instead of vanishing silently, saying
which of the two it was.

Three seams:
  1. ``MergeLane`` reports pending anchors at close (pure data structure).
  2. method-lane drop -> incident with target_type="method".
  3. action-lane drop -> incident with target_type="action".
"""

import asyncio
from collections.abc import AsyncGenerator

import orca.orca as orca
from orca.events.execution_context import WorkflowExecutionContext
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.incident_store import (
    IncidentCategory,
    IncidentSeverity,
    RecoveryAction,
    UnresolvedAnchorInsertDetail,
)
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.reservation_manager.errors import (
    ActionFailedContext,
    IRecoverableTimeoutCoordinator,
    IThreadIncidentDeclarer,
    OrphanedBacklogContext,
    UnresolvableDeadlockContext,
)
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)
from orca.workflow_models.labware_threads.labware_thread import (
    LabwareThreadInstance,
)
from orca.workflow_models.merge_lane import DroppedAnchorInsert, MergeLane
from orca.workflow_models.mutation_position import After, Before
from orca.workflow_models.status_enums import FailurePolicy, RecoveryDecision
from tests.mutation_helpers import pause_and_wait, wait_for_paused, wait_for_threads
from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    wire_system_map,
)
from tests.test_mutation_coordinator import (
    TrackingDevice,
    _build_mutation_system,
    _make_method,
    _make_thread,
)


async def _empty_gen() -> AsyncGenerator[str, None]:
    return
    yield ""  # pragma: no cover -- makes this an async generator


async def _one_item_gen(item: str) -> AsyncGenerator[str, None]:
    yield item


async def _two_item_gen(first: str, second: str) -> AsyncGenerator[str, None]:
    yield first
    yield second


class TestMergeLaneReportsUnresolvedAnchors:
    """The pure data structure surfaces pending anchors at close."""

    async def test_close_returns_dropped_before_and_after_anchors(self) -> None:
        lane: MergeLane[str] = MergeLane(_empty_gen(), name_getter=lambda s: s)
        lane.insert_before("never_runs", "early")
        lane.insert_after("also_never", "late")

        dropped = await lane.close()

        assert DroppedAnchorInsert("never_runs", "before", "early", False) in dropped
        assert DroppedAnchorInsert("also_never", "after", "late", False) in dropped
        assert len(dropped) == 2

    async def test_an_insert_left_queued_says_its_anchor_reached(self) -> None:
        """The anchor came past and the insert was still waiting when the lane
        closed. Reporting that as an anchor that never appeared sends the
        operator hunting a typo in a tag that is right."""
        lane: MergeLane[str] = MergeLane(_one_item_gen("a"), name_getter=lambda s: s)
        assert await lane.next() == "a"
        lane.insert_after("a", "queued")
        lane.insert_after("never_runs", "orphan")

        dropped = await lane.close()

        assert DroppedAnchorInsert("a", "after", "queued", True) in dropped
        assert DroppedAnchorInsert("never_runs", "after", "orphan", False) in dropped

    async def test_an_anchor_consumed_mid_group_counts_as_reached(self) -> None:
        """An insert consumed while its own anchor group still has siblings
        queued is still a step that went past. Nothing else records it."""
        lane: MergeLane[str] = MergeLane(_one_item_gen("a"), name_getter=lambda s: s)
        assert await lane.next() == "a"
        lane.insert_after("a", "x1")
        lane.insert_after("a", "x2")
        assert await lane.next() == "x1"
        lane.insert_before("x1", "chained_onto_x1")

        dropped = await lane.close()

        assert DroppedAnchorInsert("x1", "before", "chained_onto_x1", True) in dropped

    async def test_an_anchor_held_for_its_before_insert_counts_as_reached(
        self,
    ) -> None:
        """A Before insert fires because the lane reached its anchor, and the
        anchor then waits in the peeked slot. A second insert on that anchor,
        dropped right there, must not be told the anchor never appeared."""
        lane: MergeLane[str] = MergeLane(_two_item_gen("a", "b"), name_getter=lambda s: s)
        lane.insert_before("a", "p1")
        assert await lane.next() == "p1"
        lane.insert_before("a", "p2")

        dropped = await lane.close()

        assert dropped == [DroppedAnchorInsert("a", "before", "p2", True)]

    async def test_a_before_anchor_registered_too_late_says_it_ran(self) -> None:
        """Same distinction on the Before side: the step went past before the
        operator anchored to it."""
        lane: MergeLane[str] = MergeLane(_one_item_gen("a"), name_getter=lambda s: s)
        assert await lane.next() == "a"
        lane.insert_before("a", "too_late")

        dropped = await lane.close()

        assert dropped == [DroppedAnchorInsert("a", "before", "too_late", True)]

    async def test_snapshot_is_non_destructive(self) -> None:
        lane: MergeLane[str] = MergeLane(_empty_gen(), name_getter=lambda s: s)
        lane.insert_before("never_runs", "early")

        first = lane.unresolved_anchor_inserts()
        second = lane.unresolved_anchor_inserts()

        assert first == second == [
            DroppedAnchorInsert("never_runs", "before", "early", False)
        ]

    async def test_close_clears_skip_set(self) -> None:
        # close() documents that it clears all state; a stale skip must not
        # silently skip a post-close recovery insertion.
        lane: MergeLane[str] = MergeLane(_empty_gen(), name_getter=lambda s: s)
        lane.add_skip("foo")

        await lane.close()

        assert lane.should_skip("foo") is False


class _BoomDeclarer(IThreadIncidentDeclarer):
    """Declarer whose anchor-insert call always fails; the other surface
    methods are never reached on this path, so they refuse to run."""

    def declare_unresolved_anchor_insert(
        self,
        execution_id: str,
        thread_id: str,
        anchor_name: str,
        direction: str,
        target_type: str,
        item_name: str | None,
        anchor_reached: bool,
    ) -> None:
        raise RuntimeError("incident store unavailable")

    def declare_unresolvable_deadlock(
        self, execution_id: str, context: UnresolvableDeadlockContext
    ) -> None:
        raise NotImplementedError

    def declare_action_failure(
        self, execution_id: str, thread_id: str, context: ActionFailedContext
    ) -> None:
        raise NotImplementedError

    def declare_orphaned_backlog(
        self, execution_id: str, context: OrphanedBacklogContext
    ) -> None:
        raise NotImplementedError

    @property
    def recoverable_timeout_coordinator(self) -> IRecoverableTimeoutCoordinator:
        raise NotImplementedError


def test_declarer_failure_does_not_propagate() -> None:
    # The declarer runs in the thread's terminal finally; a failure must be
    # swallowed so it can't skip reservation release or mask the exit cause.
    pad = Location("pad", resource=PlatePad("pad"))
    thread = LabwareThreadInstance(
        labware=LabwareInstance("plate_96", "96_well"),
        start_location=pad,
        end_locations=[pad],
        run_mode=WorkflowRunMode.PURE_SIM,
    )

    et = ExecutingLabwareThread.__new__(ExecutingLabwareThread)
    et._thread_incident_declarer = _BoomDeclarer()
    et._context = WorkflowExecutionContext(execution_id="e1", workflow_name="w1")
    et._thread = thread

    # Must not raise.
    et._declare_unresolved_anchor_inserts(
        [DroppedAnchorInsert("a", "before", "x", False)], "method",
    )


class _RecordingDeclarer(IThreadIncidentDeclarer):
    """Captures what the thread forwards; the other surface methods are not
    reached on this path."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def declare_unresolved_anchor_insert(
        self,
        execution_id: str,
        thread_id: str,
        anchor_name: str,
        direction: str,
        target_type: str,
        item_name: str | None,
        anchor_reached: bool,
    ) -> None:
        self.calls.append((anchor_name, anchor_reached))

    def declare_unresolvable_deadlock(
        self, execution_id: str, context: UnresolvableDeadlockContext
    ) -> None:
        raise NotImplementedError

    def declare_action_failure(
        self, execution_id: str, thread_id: str, context: ActionFailedContext
    ) -> None:
        raise NotImplementedError

    def declare_orphaned_backlog(
        self, execution_id: str, context: OrphanedBacklogContext
    ) -> None:
        raise NotImplementedError

    @property
    def recoverable_timeout_coordinator(self) -> IRecoverableTimeoutCoordinator:
        raise NotImplementedError


def test_the_thread_forwards_what_the_lane_reported() -> None:
    # The lane decides; the thread must not flatten it on the way out.
    pad = Location("pad", resource=PlatePad("pad"))
    thread = LabwareThreadInstance(
        labware=LabwareInstance("plate_96", "96_well"),
        start_location=pad,
        end_locations=[pad],
        run_mode=WorkflowRunMode.PURE_SIM,
    )
    declarer = _RecordingDeclarer()
    et = ExecutingLabwareThread.__new__(ExecutingLabwareThread)
    et._thread_incident_declarer = declarer
    et._context = WorkflowExecutionContext(execution_id="e1", workflow_name="w1")
    et._thread = thread

    et._declare_unresolved_anchor_inserts(
        [
            DroppedAnchorInsert("ran", "after", "x", True),
            DroppedAnchorInsert("never", "after", "y", False),
        ],
        "method",
    )

    assert declarer.calls == [("ran", True), ("never", False)]


async def _anchor_incidents(runtime: SystemRuntime) -> list:
    return await runtime.incidents.list(category=IncidentCategory.UNRESOLVED_ANCHOR_INSERT)


class TestUnresolvedAnchorInsertIncident:

    async def test_the_incident_says_which_of_the_two_drops_it_was(self) -> None:
        """The message is the only part an operator reads. One tells them to
        go check the tag; the other tells them the run ended early. Saying the
        first when it was the second sends them hunting a typo that is not
        there."""
        f = await _build_mutation_system(method_names=["shake_1"])
        await f.runtime.start()
        try:
            f.runtime.declare_unresolved_anchor_insert(
                "e1", "t1", "shake_1", "after", "method", "extra", True,
            )
            f.runtime.declare_unresolved_anchor_insert(
                "e1", "t1", "typo_1", "after", "method", "extra", False,
            )

            messages = {i.detail.anchor_name: i.message
                        for i in await _anchor_incidents(f.runtime)
                        if isinstance(i.detail, UnresolvedAnchorInsertDetail)}

            assert "'shake_1' did appear, but the insert never ran" in (
                messages["shake_1"])
            assert "never appeared" not in messages["shake_1"]
            assert "anchor 'typo_1' never appeared on the stream" in messages["typo_1"]
        finally:
            await f.runtime.shutdown()

    async def test_method_lane_anchor_that_never_runs_fires_warning(self) -> None:
        f = await _build_mutation_system(method_names=["shake_1", "seal_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)
        await pause_and_wait(f.runtime, record.id, threads[0].id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def extra(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=180, duration=1)

        ghost = _make_method("ghost", [extra])
        f.runtime.system.insert_method(threads[0].id, ghost, where=Before("does_not_exist"))

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED
        runtime = f.runtime

        incidents = await _anchor_incidents(runtime)
        assert len(incidents) == 1, f"expected one anchor incident, got {incidents}"
        incident = incidents[0]
        assert incident.severity == IncidentSeverity.WARNING
        assert incident.recovery_action == RecoveryAction.NONE
        assert isinstance(incident.detail, UnresolvedAnchorInsertDetail)
        assert incident.detail.anchor_name == "does_not_exist"
        assert incident.detail.direction == "before"
        assert incident.detail.target_type == "method"
        assert incident.detail.item_name == "ghost"
        assert incident.detail.anchor_reached is False
        await runtime.shutdown()

    async def test_action_lane_anchor_that_never_runs_fires_warning(self) -> None:
        # Error-pause INSIDE the method so the action insert lands in that
        # method's action lane; its anchor tag never appears, so the insert
        # drops at method completion.
        device = TrackingDevice("device1")
        device.should_fail_shake = True
        transporter = create_test_transporter("robot1", ["device1", "pad1"])
        plate = create_test_plate_template("plate_96")

        registry = ResourceRegistry()
        registry.add_resource(device)
        registry.add_resource(transporter)
        pool = ResourcePool("device1", [device])
        registry.add_resource_pool(pool)
        system_map = SystemMap(registry)
        await wire_system_map(system_map, devices={"device1": device}, pads=["pad1"])

        @orca.action(device=pool, inputs=[plate], failure_policy=FailurePolicy.PAUSE)
        async def shake(ctx: ActionContext) -> None:
            await ctx.device().shake(duration=1, speed=500)

        method = _make_method("one_action", [shake])
        pad_loc = system_map.get_location("pad1")
        thread_tmpl = _make_thread(plate, pad_loc, pad_loc, [method])
        workflow = WorkflowTemplate("test_workflow")
        workflow.add_thread(thread_tmpl, is_start=True)

        event_bus = EventBus()
        builder = SdkToSystemBuilder(
            name="test_system", description="", labwares=[plate],
            resources_registry=registry, system_map=system_map,
            workflows=[workflow], event_bus=event_bus,
        )
        await builder.bind_labwares()
        runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)
        await runtime.start()
        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
        paused_id = await wait_for_paused(runtime, record.id)

        @orca.action(device=pool, inputs=[plate], tag="extra_seal")
        async def extra(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=180, duration=1)

        runtime.system.insert_action(paused_id, extra, where=After("no_such_tag"))

        device.should_fail_shake = False
        runtime.recover_thread(record.id, paused_id, RecoveryDecision.RETRY)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        incidents = await _anchor_incidents(runtime)
        assert len(incidents) == 1, f"expected one anchor incident, got {incidents}"
        incident = incidents[0]
        assert incident.severity == IncidentSeverity.WARNING
        assert isinstance(incident.detail, UnresolvedAnchorInsertDetail)
        assert incident.detail.direction == "after"
        assert incident.detail.target_type == "action"
        assert incident.detail.anchor_name == "no_such_tag"
        await runtime.shutdown()

    async def test_resolved_anchor_fires_no_incident(self) -> None:
        f = await _build_mutation_system(method_names=["shake_1", "seal_1"])
        await f.runtime.start()
        record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
        threads = await wait_for_threads(f.runtime, record.id)
        await pause_and_wait(f.runtime, record.id, threads[0].id)

        @orca.action(device=f.pool, inputs=[f.plate])
        async def extra(ctx: ActionContext) -> None:
            await ctx.device().seal(temperature=180, duration=1)

        # Anchor on seal_1, still pending while paused -- the insert resolves.
        ghost = _make_method("ghost", [extra])
        f.runtime.system.insert_method(threads[0].id, ghost, where=Before("seal_1"))

        f.runtime.resume_thread(record.id, threads[0].id)
        status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=15.0)
        assert status.status == ExecutionState.COMPLETED

        thread = f.runtime.system.get_executing_thread(threads[0].id)
        completed = [m.name for m in thread.completed_methods]
        assert "ghost" in completed, f"ghost should have run: {completed}"
        assert await _anchor_incidents(f.runtime) == []
        await f.runtime.shutdown()
