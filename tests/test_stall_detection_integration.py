"""End-to-end: a co-labware wait that never resolves is caught by the structural
stall detector and surfaced as a SYSTEM_STALL incident with the execution paused.

``co_labware_timeout`` is set well above this test's timeout budget so the
DETECTOR, not any wall-clock cap, is what fires -- proving the detector is the
mechanism that surfaces the stall.

Every wait here is event-driven; none is a sleep budget. The poll this replaced
was the flake: ``incidents.list()`` is a DB read, and at 0.1s it out-competed the
detector's own 0.25s tick for the loop, so the detector could not reach its 2
stable ticks inside a window that was nominally 16x too large. It failed ~2 in 12
pinned to one CPU. The test starved the thing it was waiting for.

The remaining timeouts are hang ceilings, not synchronization: each is well under
the 30s mark so the wait, not pytest, reports which step stalled. If this file
ever needs a sleep to pass, the engine is the bug -- add the missing event.
"""
import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest

import orca.orca as orca
from orca.config import CoordinationConfig, OrcaConfig
from orca.events.event_bus import EventBus
from orca.runtime.incident_store import IncidentCategory, SystemStallDetail
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.stall_detector import SystemStallError, ThreadStallSnapshot
from orca.runtime.system_runtime import SystemRuntime
from orca.resource_models.resource_pool import ResourcePool
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.status_enums import LabwareThreadStatus
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext

from tests.mock import UniversalMockDevice
from tests.test_helpers import (
    execution_outcome,
    create_test_plate_template,
    create_test_transporter,
    wait_for_paused_threads,
    wait_for_runtime_condition,
    wire_system_map,
)


class _FakeLabware:
    def __init__(self, name: str) -> None:
        self.name = name


async def _build_stuck_runtime(
    stall_check_interval: float | None,
) -> tuple[SystemRuntime, WorkflowTemplate]:
    device = UniversalMockDevice("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    registry.add_resource_pool(ResourcePool("shaker1", [device]))

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    @orca.action(device=registry.get_resource_pool("shaker1"), inputs=[plate])
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def stuck_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield shake_action

    pad_loc = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
    async def plate_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield stuck_method

    workflow = WorkflowTemplate("stall_test")
    workflow.add_thread(plate_thread, is_start=True)

    event_bus = EventBus()
    # co_labware_timeout well above the test budget so it never fires; the short
    # detector interval is what surfaces the never-arriving co-labware.
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
        config=OrcaConfig(coordination=CoordinationConfig(co_labware_timeout=600.0)),
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(
        builder.get_system(), event_bus=event_bus,
        stall_check_interval=stall_check_interval,
    )
    return runtime, workflow


def _patch_action_to_never_fire(system: ISystem) -> bool:
    for t in system.executing_threads:
        if t.assigned_action is not None:
            action = t.assigned_action.action
            action._all_labware_is_present = asyncio.Event()
            setattr(action, "peek_missing_input_labware",
                    lambda: [_FakeLabware("phantom_plate")])
            return True
    return False


@pytest.mark.asyncio
async def test_never_arriving_co_labware_surfaces_stall_incident() -> None:
    runtime, _ = await _build_stuck_runtime(stall_check_interval=0.25)
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            "stall_test", mode=WorkflowRunMode.PURE_SIM)

        # Patching is the predicate: the action becomes assignable on a status
        # transition, so this lands on that event rather than on a sleep guess.
        await wait_for_runtime_condition(
            runtime,
            lambda: _patch_action_to_never_fire(runtime.system),
            timeout=15.0,
            message="action never resolved, cannot force the co-labware wait",
        )

        # _handle_stall records before it pauses and list() flushes pending, so the
        # PAUSE transition is where the incident becomes readable.
        paused = await wait_for_paused_threads(runtime, record.id, count=1, timeout=15.0)
        incidents = await runtime.incidents.list(
            category=IncidentCategory.SYSTEM_STALL)

        assert incidents, "stall detector never surfaced a SYSTEM_STALL"
        incident = incidents[0]
        assert incident.execution_id == record.id
        assert isinstance(incident.detail, SystemStallDetail)
        assert any("phantom_plate" in w for w in incident.detail.waits), (
            f"incident should name the missing labware: {incident.detail.waits}"
        )

        # The response breaks the co-labware wait into a recoverable PAUSED state
        # rather than leaving the thread hung.
        assert all(t.status == LabwareThreadStatus.PAUSED for t in paused), (
            f"stalled thread was not paused for operator recovery: {paused}"
        )

        # Exactly one incident per episode: the report-once latch (and the pause
        # moving threads out of the candidate set) prevents per-tick spam.
        again = await runtime.incidents.list(category=IncidentCategory.SYSTEM_STALL)
        assert len(again) == 1, f"stall must report once per episode, got {len(again)}"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_wait_for_execution_raises_typed_stall_error() -> None:
    """A detected stall fails waiters within seconds via SystemStallError
    instead of blocking blind to an external timeout. The execution itself
    stays PAUSED and operator-recoverable; only the awaiter is released."""
    runtime, workflow = await _build_stuck_runtime(stall_check_interval=0.25)
    await runtime.start()
    try:
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await wait_for_runtime_condition(
            runtime,
            lambda: _patch_action_to_never_fire(runtime.system),
            timeout=15.0,
            message="action never resolved, cannot force the co-labware wait",
        )

        with pytest.raises(SystemStallError) as excinfo:
            await execution_outcome(runtime, submission, timeout=15.0)
        assert "phantom_plate" in str(excinfo.value), (
            f"the stall error must carry the wait diagnostic: {excinfo.value}"
        )
        # The awaiter failed fast, but the execution is still recoverable.
        paused = await wait_for_paused_threads(
            runtime, submission.execution_id, count=1, timeout=5.0)
        assert all(t.status == LabwareThreadStatus.PAUSED for t in paused), (
            f"stalled threads must stay PAUSED for operator recovery: {paused}"
        )
        # The wait() surface fails the same way instead of blocking blind.
        with pytest.raises(SystemStallError):
            await asyncio.wait_for(
                runtime.wait(submission.execution_id), timeout=5.0)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_stall_detection_is_per_execution(monkeypatch) -> None:
    """A healthy execution (in-flight thread) must NOT mask a stalled sibling.

    Drives ``_check_for_stalls`` directly with two executions -- one wedged in
    co-labware, one progressing -- and asserts only the wedged one is handled.
    Guards the regression where global (all-executions) detection let a healthy
    execution suppress a stalled one.
    """
    runtime, _ = await _build_stuck_runtime(stall_check_interval=None)
    monkeypatch.setattr(runtime, "_executions", {
        "healthy": SimpleNamespace(
            stall_event=asyncio.Event(), stall_report=None, paused_at=None),
        "wedged": SimpleNamespace(
            stall_event=asyncio.Event(), stall_report=None, paused_at=None),
    })
    snapshots = {
        "healthy": [ThreadStallSnapshot("h1", LabwareThreadStatus.EXECUTING_ACTION, None)],
        "wedged": [ThreadStallSnapshot("w1", LabwareThreadStatus.AWAITING_CO_THREADS, "tips")],
    }
    monkeypatch.setattr(runtime, "_build_execution_snapshots", lambda eid: snapshots[eid])
    handled: list[str] = []
    monkeypatch.setattr(runtime, "_handle_stall", lambda eid, report: handled.append(eid))

    runtime._check_for_stalls()   # tick 1
    runtime._check_for_stalls()   # tick 2 -> wedged settles
    assert handled == ["wedged"], (
        f"only the wedged execution should stall; the healthy one must not be "
        f"masked nor falsely flagged. handled={handled}"
    )


@pytest.mark.asyncio
async def test_resume_clears_the_stall_episode_and_rearms_the_detector() -> None:
    """Operator resume ends the stall episode: the waiter event clears so a
    fresh wait does not insta-raise on a recovered execution, and the detector
    re-arms so a wedge persisting after resume fails waiters as a NEW episode."""
    runtime, workflow = await _build_stuck_runtime(stall_check_interval=0.25)
    await runtime.start()
    try:
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await wait_for_runtime_condition(
            runtime,
            lambda: _patch_action_to_never_fire(runtime.system),
            timeout=15.0,
            message="action never resolved, cannot force the co-labware wait",
        )
        with pytest.raises(SystemStallError):
            await execution_outcome(runtime, submission, timeout=15.0)

        execution = runtime._executions[submission.execution_id]
        assert execution.paused_at is not None, (
            "the stall must latch the execution-level pause gate"
        )
        runtime.resume_execution(submission.execution_id)
        assert not execution.stall_event.is_set(), "resume must clear the episode"
        assert submission.execution_id not in runtime._stall_detectors, (
            "resume must re-arm the detector"
        )
        assert execution.paused_at is None, "resume must lift the submission gate"

        # Still wedged after resume: a fresh episode fires for the new waiter.
        with pytest.raises(SystemStallError):
            await execution_outcome(runtime, submission, timeout=15.0)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_declared_deadlock_latches_the_submission_gate(monkeypatch) -> None:
    """declare_unresolvable_deadlock pauses the whole EXECUTION, not just its
    threads: the latch refuses JOIN_EXISTING until resume, same as a detected
    stall. Same operator-recovery family as the stall levers in this file."""
    from orca.system.reservation_manager.errors import UnresolvableDeadlockContext

    runtime, _ = await _build_stuck_runtime(stall_check_interval=None)
    execution = SimpleNamespace(
        stall_event=asyncio.Event(), stall_report=None, paused_at=None)
    monkeypatch.setattr(runtime, "_executions", {"e1": execution})
    fanned: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        runtime, "pause_all_threads",
        lambda eid, reason="manual", message=None: fanned.append((eid, reason, message)) or {})

    incident = runtime.declare_unresolvable_deadlock(
        "e1",
        UnresolvableDeadlockContext(
            requesting_thread_id="t-requester",
            requesting_labware_id="lw-requester",
            blocking_position_id="pad1",
            blocking_thread_id="t-blocker",
            blocking_labware_id="lw-blocker",
            reason="immovable_blocker",
            hint="unit pin",
        ),
    )

    assert incident.execution_id == "e1"
    assert execution.paused_at is not None, (
        "a declared deadlock must latch the submission gate"
    )
    assert fanned[0][:2] == ("e1", "system"), (
        "a declared deadlock is not an operator pausing the run; the thread "
        "state must say so, not read as manual"
    )
    assert fanned[0][2] == incident.message, (
        "the pause must carry the deadlock's own cause, not leave the "
        "operator to guess from 'system' alone"
    )


@pytest.mark.asyncio
async def test_stop_confirm_on_a_stalled_execution_returns_aborted() -> None:
    """Stop is the stall incident's documented recovery: the confirmed abort
    ends the episode and reports aborted instead of re-raising the stall out
    of the operator verb."""
    runtime, workflow = await _build_stuck_runtime(stall_check_interval=0.25)
    await runtime.start()
    try:
        submission = await runtime.submit(workflow, mode=WorkflowRunMode.PURE_SIM)
        await wait_for_runtime_condition(
            runtime,
            lambda: _patch_action_to_never_fire(runtime.system),
            timeout=15.0,
            message="action never resolved, cannot force the co-labware wait",
        )
        with pytest.raises(SystemStallError):
            await execution_outcome(runtime, submission, timeout=15.0)

        armed = await runtime.stop_execution(submission.execution_id)
        assert armed.armed and not armed.aborted
        outcome = await asyncio.wait_for(
            runtime.stop_execution(submission.execution_id, confirm=True),
            timeout=10.0,
        )
        assert outcome.aborted, "confirmed stop of a stalled execution must abort"
        assert not runtime._executions[submission.execution_id].stall_event.is_set(), (
            "abort must end the stall episode"
        )
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_reservation_wait_on_a_busy_sibling_execution_is_not_a_stall(monkeypatch) -> None:
    """The mixed rule's premise (only an acting thread releases a reservation)
    breaks ACROSS executions: a sibling execution's in-flight thread may hold
    the reserved device and releases it by finishing. Declaring the waiting
    execution stalled pauses it permanently under a live system -- the false
    positive that wedged concurrent-standalone SMC runs. Reservation waits
    join the stall shape only when the whole SYSTEM is quiescent; a pure
    co-labware stall stays per-execution (the anti-masking rule stands)."""
    runtime, _ = await _build_stuck_runtime(stall_check_interval=None)
    monkeypatch.setattr(runtime, "_executions", {
        "busy": SimpleNamespace(stall_event=asyncio.Event(), stall_report=None),
        "waiting": SimpleNamespace(stall_event=asyncio.Event(), stall_report=None),
    })
    snapshots = {
        "busy": [ThreadStallSnapshot("b1", LabwareThreadStatus.EXECUTING_ACTION, None)],
        "waiting": [
            ThreadStallSnapshot("w1", LabwareThreadStatus.AWAITING_CO_THREADS, "tips"),
            ThreadStallSnapshot("w2", LabwareThreadStatus.AWAITING_ACTION_RESERVATION, "bravo_384"),
        ],
    }
    monkeypatch.setattr(runtime, "_build_execution_snapshots", lambda eid: snapshots[eid])
    handled: list[str] = []
    monkeypatch.setattr(runtime, "_handle_stall", lambda eid, report: handled.append(eid))

    runtime._check_for_stalls()   # tick 1
    runtime._check_for_stalls()   # tick 2
    assert handled == [], (
        f"a reservation wait on hardware a busy sibling holds is contention, "
        f"not a stall; handled={handled}"
    )

    # The sibling wedges too: now nothing anywhere can act, and the same
    # mixed shape is a genuine system-wide stall.
    snapshots["busy"] = [
        ThreadStallSnapshot("b1", LabwareThreadStatus.AWAITING_CO_THREADS, "plate"),
    ]
    runtime._check_for_stalls()   # tick 1 of the new signature
    runtime._check_for_stalls()   # tick 2 -> both settle
    assert set(handled) == {"busy", "waiting"}, (
        f"once the whole system is quiescent the mixed shape must declare; "
        f"handled={handled}"
    )


@pytest.mark.asyncio
async def test_mixed_stall_incident_message_covers_both_wait_kinds(monkeypatch) -> None:
    """A mixed stall (co-labware waiter + reservation waiter, none in flight)
    must surface through the same incident + pause plumbing, and the incident
    message must describe the actual shape -- not claim every thread is
    "blocked on co-labware" when one is in a reservation wait."""
    runtime, _ = await _build_stuck_runtime(stall_check_interval=None)
    # _handle_stall pauses the execution (reads paused_at) and sets
    # stall_report/stall_event on the execution entry.
    fake_execution = SimpleNamespace(
        stall_event=asyncio.Event(), stall_report=None, paused_at=None)
    monkeypatch.setattr(runtime, "_executions", {"mixed": fake_execution})
    snapshots = [
        ThreadStallSnapshot("owner", LabwareThreadStatus.AWAITING_CO_THREADS, "tips"),
        ThreadStallSnapshot("mover", LabwareThreadStatus.AWAITING_MOVE_RESERVATION, "pad1"),
    ]
    monkeypatch.setattr(runtime, "_build_execution_snapshots", lambda eid: snapshots)
    paused: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runtime, "pause_all_threads",
        lambda eid, reason="manual", message=None: paused.append((eid, reason)))

    runtime._check_for_stalls()   # tick 1
    runtime._check_for_stalls()   # tick 2 -> mixed shape settles

    incidents = await runtime.incidents.list(category=IncidentCategory.SYSTEM_STALL)
    assert incidents, "mixed stall never surfaced a SYSTEM_STALL incident"
    incident = incidents[0]
    assert isinstance(incident.detail, SystemStallDetail)
    assert set(incident.detail.stalled_thread_ids) == {"owner", "mover"}
    assert "blocked on co-labware with none in flight" not in incident.message, (
        "the incident message must not describe a reservation waiter as "
        f"blocked on co-labware: {incident.message}"
    )
    assert "none in flight" in incident.message
    assert paused == [("mixed", "system")], (
        "a stall is not an operator pausing the run; the thread state must "
        "say so, not read as manual"
    )


async def _build_joined_stuck_runtime() -> tuple[SystemRuntime, WorkflowTemplate]:
    """An owner runs a shared action; a contributor joins it and parks.

    The owner then waits for co-labware that never arrives, so nothing in the
    execution can move: the contributor is only holding for the owner's outcome.
    """
    device = UniversalMockDevice("dev1", site_names=["site-1", "site-2"])
    transporter = create_test_transporter("robot1", ["dev1", "pad1", "pad2"])
    plate_main = create_test_plate_template("plate_main")
    plate_child = create_test_plate_template("plate_child")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("dev1", [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"dev1": device}, pads=["pad1", "pad2"])

    @orca.action(device=pool, inputs=[plate_main, plate_child])
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def shared_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake_action

    pad1 = system_map.get_location("pad1")
    pad2 = system_map.get_location("pad2")

    @orca.thread(labware=plate_main, start=pad1, end=pad1)
    async def owner_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        del ctx
        yield shared_method

    @orca.thread(labware=plate_child, start=pad2, end=pad2)
    async def contributor_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield orca.join(allows=[shared_method])

    @orca.workflow(name="joined_stall_test")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(owner_thread)
        wf.thread(contributor_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system", description="", labwares=[plate_main, plate_child],
        resources_registry=registry, system_map=system_map,
        workflows=[workflow], event_bus=event_bus,
        config=OrcaConfig(coordination=CoordinationConfig(co_labware_timeout=600.0)),
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(
        builder.get_system(), event_bus=event_bus, stall_check_interval=0.25)
    return runtime, workflow


def _patch_owner_action_to_never_fire(system: ISystem) -> bool:
    for thread in system.executing_threads:
        if "plate_main" not in thread.labware.name or thread.assigned_action is None:
            continue
        action = thread.assigned_action.action
        action._all_labware_is_present = asyncio.Event()
        setattr(action, "peek_missing_input_labware",
                lambda: [_FakeLabware("phantom_plate")])
        return True
    return False


def _a_contributor_is_parked(system: ISystem) -> bool:
    return any(t.following_peer_action for t in system.executing_threads)


@pytest.mark.asyncio
async def test_a_parked_contributor_does_not_hide_the_stall() -> None:
    """A contributor awaiting the owner's outcome reads EXECUTING_ACTION, which
    used to count as progress and silence the detector for the whole execution.
    It drives nothing and holds nothing, so it must not mask a wedged owner."""
    runtime, _ = await _build_joined_stuck_runtime()
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            "joined_stall_test", mode=WorkflowRunMode.PURE_SIM)
        await wait_for_runtime_condition(
            runtime,
            lambda: _patch_owner_action_to_never_fire(runtime.system),
            timeout=20.0,
            message="owner action never resolved, cannot force the co-labware wait",
        )
        await wait_for_runtime_condition(
            runtime,
            lambda: _a_contributor_is_parked(runtime.system),
            timeout=20.0,
            message="contributor never joined the shared action",
        )

        await wait_for_paused_threads(runtime, record.id, count=1, timeout=20.0)
        incidents = await runtime.incidents.list(
            category=IncidentCategory.SYSTEM_STALL)
        assert incidents, (
            "a parked contributor masked the stall: no SYSTEM_STALL was raised"
        )
        assert isinstance(incidents[0].detail, SystemStallDetail)
        assert any("phantom_plate" in w for w in incidents[0].detail.waits), (
            f"the report must name the wait: {incidents[0].detail.waits}"
        )
    finally:
        await runtime.shutdown()
