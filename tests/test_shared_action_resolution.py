"""Shared-action recovery under the authoritative-resolution model.

Contract: a bound shared-action device failure pauses the whole action group -- the
owner AND every joined contributor -- because an action is one device and all labware
converged on it is physically stuck. The owner records the ONE incident (root cause)
and drives the single authoritative recovery; contributors record nothing and fan out
to the owner's one outcome. Recovering ANY participant feeds that one decision (first
wins), so no thread half-resumes; RETRY/RETRY_OP re-drive on the owner and every
participant rides along. (A pre-binding resolution failure places no labware at the
device, so only the owner pauses there.)
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.events.event_bus import EventBus
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import IMethodTemplate
from orca.workflow_models.status_enums import RecoveryDecision
from orca.runtime.status_models import ThreadSnapshot
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext

from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    wait_for_paused_threads,
    wire_system_map,
)
from tests.test_co_thread_rendezvous_collapse import _build_two_action_join_system
from tests.test_stop_cancel_and_cascade import (
    _FailOnceDevice,
    _build_join_system,
    _wait_for_shared_rendezvous,
)


def _statuses(runtime, eid: str) -> list[str]:
    return sorted(t.status for t in runtime.list_threads(eid))


class _SitedFailOnceDevice(_FailOnceDevice):
    """_FailOnceDevice with declared working sites so a multi-input convergence
    passes the single-occupancy site-count check."""

    def __init__(self, name: str, site_names: list[str]) -> None:
        super().__init__(name)
        self._site_names = list(site_names)


async def _build_multi_contributor_join_system() -> tuple[
    SystemRuntime, WorkflowTemplate, _FailOnceDevice
]:
    """One owner runs a three-input shake; two contributors join it, so a single
    shared action has one owner and two contributors on the same slot."""
    device = _SitedFailOnceDevice("shaker1", ["site-1", "site-2", "site-3"])
    transporter = create_test_transporter("robot1", ["shaker1", "pad1", "pad2", "pad3"])
    plate_main = create_test_plate_template("plate_main")
    plate_child1 = create_test_plate_template("plate_child1")
    plate_child2 = create_test_plate_template("plate_child2")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(
        system_map, devices={"shaker1": device}, pads=["pad1", "pad2", "pad3"],
    )

    @orca.action(device=pool, inputs=[plate_main, plate_child1, plate_child2])
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.method
    async def parent_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake_action

    pad1 = system_map.get_location("pad1")
    pad2 = system_map.get_location("pad2")
    pad3 = system_map.get_location("pad3")

    @orca.thread(labware=plate_main, start=pad1, end=pad1)
    async def main_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield parent_method

    @orca.thread(labware=plate_child1, start=pad2, end=pad2)
    async def child1_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield orca.join()

    @orca.thread(labware=plate_child2, start=pad3, end=pad3)
    async def child2_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield orca.join()

    @orca.workflow(name="multi_join_fail_wf")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(main_thread)
        wf.thread(child1_thread)
        wf.thread(child2_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system",
        description="",
        labwares=[plate_main, plate_child1, plate_child2],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)
    return runtime, workflow, device


async def _wait_until(predicate, timeout: float = 12.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("condition not reached within timeout")


async def test_shared_device_failure_pauses_every_participant() -> None:
    runtime, workflow, _ = await _build_join_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        await _wait_for_shared_rendezvous(runtime, record.id)
        paused = runtime.get_paused_threads(record.id)
        assert len(paused) == 2, (
            f"a bound shared-action failure pauses the whole action group (owner + "
            f"contributor), so the operator sees the whole action stuck. got "
            f"{_statuses(runtime, record.id)}"
        )
        sites = {t.pause_site for t in paused}
        assert sites == {"DEVICE_OP"}, (
            "the owner stopped inside the shake, and a contributor drives no "
            "call at all -- but the group takes ONE decision judged against the "
            "OWNER's site, so both have to report it. A blank on the "
            "contributor is a client with no way to know RETRY_OP is legal "
            f"from there; got {sites}"
        )
    finally:
        await runtime.abort_execution(record.id)
        await runtime.shutdown()


async def test_abort_thread_on_owner_aborts_every_participant() -> None:
    runtime, workflow, _ = await _build_join_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        await _wait_for_shared_rendezvous(runtime, record.id)
        owner = runtime.get_paused_threads(record.id)[0]
        runtime.recover_thread(record.id, owner.id, RecoveryDecision.ABORT_THREAD)
        await _wait_until(
            lambda: runtime.get_execution(record.id).status != ExecutionState.RUNNING
        )
        assert _statuses(runtime, record.id) == ["ABORTED", "ABORTED"], (
            f"ABORT_THREAD on the owner must fan out so every participant lands "
            f"ABORTED; got {_statuses(runtime, record.id)}"
        )
    finally:
        try:
            await asyncio.wait_for(runtime.wait(record.id), timeout=8.0)
        except Exception:
            await runtime.abort_execution(record.id)
        await runtime.shutdown()


async def test_abort_method_on_owner_continues_every_participant() -> None:
    runtime, workflow, _ = await _build_join_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        await _wait_for_shared_rendezvous(runtime, record.id)
        owner = runtime.get_paused_threads(record.id)[0]
        runtime.recover_thread(record.id, owner.id, RecoveryDecision.ABORT_METHOD)
        final = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert final.status == ExecutionState.COMPLETED, (
            f"ABORT_METHOD abandons the shared method; both participants continue "
            f"their journey to completion. got {_statuses(runtime, record.id)}"
        )
    finally:
        await runtime.shutdown()


async def test_retry_on_owner_redrives_and_every_participant_completes() -> None:
    runtime, workflow, device = await _build_join_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        await _wait_for_shared_rendezvous(runtime, record.id)
        device.should_fail = False
        owner = runtime.get_paused_threads(record.id)[0]
        runtime.recover_thread(record.id, owner.id, RecoveryDecision.RETRY)
        final = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert final.status == ExecutionState.COMPLETED, (
            f"RETRY on the owner re-drives the shared action; the contributor rides "
            f"along to completion. got {_statuses(runtime, record.id)}"
        )
    finally:
        await runtime.shutdown()


async def test_shared_failure_records_exactly_one_incident() -> None:
    runtime, workflow, _ = await _build_join_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        await _wait_for_shared_rendezvous(runtime, record.id)
        incidents = await runtime.incidents.list(execution_id=record.id)
        assert len(incidents) == 1, (
            f"only the owner records an incident on a shared failure; contributors "
            f"fan out to its outcome. got {[(i.category, i.message) for i in incidents]}"
        )
    finally:
        await runtime.abort_execution(record.id)
        await runtime.shutdown()


async def test_abort_action_advances_to_next_shared_action() -> None:
    runtime, workflow, device = await _build_two_action_join_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        await _wait_for_shared_rendezvous(runtime, record.id)
        assert device.shake_count == 1 and device.seal_count == 0
        owner = runtime.get_paused_threads(record.id)[0]
        runtime.recover_thread(record.id, owner.id, RecoveryDecision.ABORT_ACTION)
        final = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert final.status == ExecutionState.COMPLETED
        assert device.seal_count >= 1, (
            "ABORT_ACTION advances the shared method to its next action (seal); "
            "the contributor fans out per slot and rides along to completion."
        )
    finally:
        await runtime.shutdown()


async def test_multi_contributor_shared_failure_one_incident_and_abort_fans_out() -> None:
    runtime, workflow, _ = await _build_multi_contributor_join_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        await _wait_for_shared_rendezvous(runtime, record.id, contributors=2)
        paused = runtime.get_paused_threads(record.id)
        assert len(paused) == 3, (
            f"one owner, two contributors on one action: all three pause as a group. "
            f"got {_statuses(runtime, record.id)}"
        )
        incidents = await runtime.incidents.list(execution_id=record.id)
        assert len(incidents) == 1, (
            f"one incident on the owner; neither contributor records one. got "
            f"{[(i.category, i.message) for i in incidents]}"
        )
        runtime.recover_thread(record.id, paused[0].id, RecoveryDecision.ABORT_THREAD)
        await _wait_until(
            lambda: runtime.get_execution(record.id).status != ExecutionState.RUNNING
        )
        assert _statuses(runtime, record.id) == ["ABORTED", "ABORTED", "ABORTED"], (
            f"ABORT_THREAD on the owner fans out to both contributors; all three land "
            f"ABORTED. got {_statuses(runtime, record.id)}"
        )
    finally:
        try:
            await asyncio.wait_for(runtime.wait(record.id), timeout=8.0)
        except Exception:
            await runtime.abort_execution(record.id)
        await runtime.shutdown()


@pytest.mark.parametrize(
    "decision",
    [
        RecoveryDecision.ABORT_THREAD,
        RecoveryDecision.ABORT_METHOD,
        RecoveryDecision.ABORT_ACTION,
    ],
)
async def test_pre_binding_abort_decision_tears_group_down_cleanly(
    monkeypatch, decision: RecoveryDecision
) -> None:
    """A pre-binding action-resolution failure has no bound action to skip, so any
    non-RETRY recovery on the owner tears the shared rendezvous down cleanly: the
    execution reaches a terminal state, the recovered owner lands ABORTED, and nothing
    is left as a zombie PAUSED thread. Before the fix ABORT_METHOD / ABORT_ACTION left
    the owner (and a parked contributor) stuck at PAUSED with the execution FAILED,
    while only ABORT_THREAD tore down cleanly. (A future SKIP will instead let the
    group abandon the step and continue.)"""
    from orca.resource_models.labware import LabwareInstance
    from orca.resource_models.location import Location
    from orca.system.reservation_manager.errors import ActionReservationTimeoutError
    from orca.workflow_models.actions.dynamic_resource_action import (
        DynamicResourceActionResolver,
    )
    from orca.workflow_models.actions.executable_location_action import (
        ExecutableLocationAction,
    )
    from orca.workflow_models.actions.util import IActionReservationStatusSink
    from orca.workflow_models.method import ExecutingMethod

    runtime, workflow, _ = await _build_join_system()

    async def _fail_resolution(
        self: ExecutingMethod,
        thread_id: str,
        current_location: Location,
        action_resolver: DynamicResourceActionResolver,
        requesting_labware: LabwareInstance | None = None,
        status_sink: IActionReservationStatusSink | None = None,
    ) -> ExecutableLocationAction:
        raise ActionReservationTimeoutError(thread_id, 0.0, ["shaker1"], "rejected")

    monkeypatch.setattr(ExecutingMethod, "resolve_current_action", _fail_resolution)

    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        paused = await wait_for_paused_threads(runtime, record.id)
        assert len(paused) == 1, (
            f"only the owner pauses at resolution; the contributor is parked in "
            f"wait_for_current_action. got {_statuses(runtime, record.id)}"
        )
        owner_id = paused[0].id
        runtime.recover_thread(record.id, owner_id, decision)
        await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert not runtime.get_paused_threads(record.id), (
            f"a pre-binding {decision.name} must leave no zombie PAUSED thread; got "
            f"{[t.name for t in runtime.get_paused_threads(record.id)]}"
        )
        owner = next(t for t in runtime.list_threads(record.id) if t.id == owner_id)
        assert owner.status == "ABORTED", (
            f"the owner must abort cleanly at a pre-binding {decision.name}, not "
            f"zombie-park at PAUSED; got {owner.status}"
        )
    finally:
        try:
            await asyncio.wait_for(runtime.wait(record.id), timeout=8.0)
        except Exception:
            await runtime.abort_execution(record.id)
        await runtime.shutdown()


@pytest.mark.parametrize(
    "decision",
    [
        RecoveryDecision.ABORT_THREAD,
        RecoveryDecision.ABORT_METHOD,
        RecoveryDecision.ABORT_ACTION,
    ],
)
async def test_a_consume_failure_aborts_rather_than_fails_the_thread(
    monkeypatch, decision: RecoveryDecision
) -> None:
    """The OTHER action-resolution handler, which used to answer differently.

    Two handlers stamp `PauseSite.ACTION_RESOLUTION`. The sibling test above
    covers `resolve_current_action`, which always aborted cleanly. This one
    covers `consume_next_unresolved_action`, which ran `_check_abort_thread`
    (ABORT_THREAD only) and then re-raised, so ABORT_ACTION and ABORT_METHOD
    FAILED the thread and took the execution with it -- at a site whose own
    `HONOURED_DECISIONS` entry says the narrower aborts end the thread instead.
    """
    from orca.workflow_models.actions.dynamic_resource_action import (
        DynamicResourceActionResolver,
    )
    from orca.workflow_models.method import ExecutingMethod

    runtime, workflow, _ = await _build_join_system()

    async def _fail_consume(
        self: ExecutingMethod, action_resolver: DynamicResourceActionResolver,
    ) -> None:
        raise RuntimeError("the lane could not be read")

    monkeypatch.setattr(
        ExecutingMethod, "consume_next_unresolved_action", _fail_consume,
    )

    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        paused = await wait_for_paused_threads(runtime, record.id)
        assert paused, f"nothing paused; got {_statuses(runtime, record.id)}"
        recovered = paused[0].id
        runtime.recover_thread(record.id, recovered, decision)
        final = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)

        thread = next(t for t in runtime.list_threads(record.id) if t.id == recovered)
        assert thread.status == "ABORTED", (
            f"{decision.name} at a consume failure must end the thread, not fail "
            f"it; got {thread.status}"
        )
        assert final.status is not ExecutionState.FAILED, (
            f"{decision.name} must not take the execution down; got {final.status}"
        )
    finally:
        try:
            await asyncio.wait_for(runtime.wait(record.id), timeout=8.0)
        except Exception:
            await runtime.abort_execution(record.id)
        await runtime.shutdown()


def _contributor(paused: list[ThreadSnapshot]) -> ThreadSnapshot:
    """The paused CONTRIBUTOR: it carries the sympathetic ``_SharedActionPausedError``
    ("... failed on the owner"), while the owner carries the real device failure."""
    return next(p for p in paused if "on the owner" in (p.last_error or ""))


async def test_recovering_the_contributor_propagates_to_the_whole_action() -> None:
    """One-propagates: a recovery decision fed via the CONTRIBUTOR (not the owner) is
    the action's single decision. RETRY submitted on the contributor re-drives the
    device op on the owner and every participant fans out to completion."""
    runtime, workflow, device = await _build_join_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        paused = await _wait_for_shared_rendezvous(runtime, record.id)
        device.should_fail = False
        runtime.recover_thread(
            record.id, _contributor(paused).id, RecoveryDecision.RETRY
        )
        final = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert final.status == ExecutionState.COMPLETED, (
            f"RETRY fed via the contributor must re-drive the shared action on the "
            f"owner and complete the whole group. got {_statuses(runtime, record.id)}"
        )
        assert device.shake_count == 2
    finally:
        await runtime.shutdown()


async def test_recovering_the_contributor_aborts_the_whole_action() -> None:
    """The decision is action-scoped, not per-thread: ABORT_THREAD fed via the
    CONTRIBUTOR tears the whole action group down -- owner AND contributor both land
    ABORTED, with no owner left stranded."""
    runtime, workflow, _ = await _build_join_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        paused = await _wait_for_shared_rendezvous(runtime, record.id)
        runtime.recover_thread(
            record.id, _contributor(paused).id, RecoveryDecision.ABORT_THREAD
        )
        await _wait_until(
            lambda: runtime.get_execution(record.id).status != ExecutionState.RUNNING
        )
        assert _statuses(runtime, record.id) == ["ABORTED", "ABORTED"], (
            f"ABORT_THREAD on the contributor must tear the whole action down; got "
            f"{_statuses(runtime, record.id)}"
        )
    finally:
        try:
            await asyncio.wait_for(runtime.wait(record.id), timeout=8.0)
        except Exception:
            await runtime.abort_execution(record.id)
        await runtime.shutdown()


async def test_each_action_pauses_its_own_group_afresh() -> None:
    """Per-action coordination: a fresh action group is minted per slot, so a failure in
    the method's SECOND action pauses the group afresh, independent of the first action's
    already-resolved group. A shared shake succeeds, then the seal fails and re-pauses the
    still-co-located participants -- proving the pause/recovery state is action-scoped."""
    from tests.mock import UniversalMockDevice
    from tests.test_confidence_gaps import _build_auto_spawn_system

    class _FailSealDevice(UniversalMockDevice):
        async def seal(self, temperature: int, duration: float) -> None:
            raise RuntimeError("Simulated seal failure")

    runtime, workflow, _, _ = await _build_auto_spawn_system(
        device=_FailSealDevice("shaker1", site_names=["site-1", "site-2"])
    )
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        await _wait_for_shared_rendezvous(runtime, record.id)
        # The shake already resolved; the SEAL (second action) is the one paused now.
        paused = runtime.get_paused_threads(record.id)
        assert all("seal" in (p.last_error or "") for p in paused), (
            f"the second action (seal) must be the one paused; got "
            f"{[(p.name, p.last_error) for p in paused]}"
        )
    finally:
        await runtime.abort_execution(record.id)
        await runtime.shutdown()


async def test_recovering_the_contributor_with_retry_op_redrives_the_action() -> None:
    """RETRY_OP fed via the CONTRIBUTOR is valid on the whole paused group: only the owner
    physically drives the device op, but the group carries the op-pause context, so an
    operator can re-drive the failed op by recovering ANY participant. The owner re-runs
    just the failed call and every participant fans out to completion."""
    runtime, workflow, device = await _build_join_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        paused = await _wait_for_shared_rendezvous(runtime, record.id)
        device.should_fail = False
        runtime.recover_thread(
            record.id, _contributor(paused).id, RecoveryDecision.RETRY_OP
        )
        final = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert final.status == ExecutionState.COMPLETED, (
            f"RETRY_OP fed via the contributor must re-drive the op on the owner and "
            f"complete the whole group. got {_statuses(runtime, record.id)}"
        )
        assert device.shake_count == 2
    finally:
        await runtime.shutdown()


async def test_group_decision_is_first_wins() -> None:
    """The group's decision channel is set-once: the first submitted decision wins and a
    later conflicting submit is a no-op, so recovering a second paused participant can never
    override the action's single authoritative decision (the no-half-resume guarantee)."""
    from orca.workflow_models.shared_action_coordination import SharedActionCoordination

    group = SharedActionCoordination()
    group.submit_decision(RecoveryDecision.ABORT_ACTION)
    group.submit_decision(RecoveryDecision.ABORT_THREAD)
    assert group.decision == RecoveryDecision.ABORT_ACTION
    assert group.decision_ready.is_set()
