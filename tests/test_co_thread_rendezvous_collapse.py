"""A terminal recovery decision on the OWNER of a shared-step rendezvous tears
the rendezvous down so every participant reaches the right terminal state, driven
by the owner's single published slot outcome (never a per-contributor recovery).

Contract (authoritative-resolution model):

- A shared action needs N labware on one device. The OWNER (the thread that
  yielded the method directly, not via ``orca.join``) drives the device and is
  the sole pauser; contributors await its published slot outcome and fan out to it.
- ABORT_THREAD on the owner -> every participant leaves ABORTED.
- ABORT_ACTION on the owner -> the method advances to its NEXT shared action
  (not terminal); contributors ride along.
- ABORT_METHOD on the owner -> every participant abandons the method and
  continues its own journey.
- A pre-binding owner failure (the owner fails at action RESOLUTION, before
  binding a current action) still reaches a contributor parked in
  ``wait_for_current_action`` via the slot outcome.
"""

import asyncio
from collections.abc import AsyncGenerator

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
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext

from tests.mock import UniversalMockDevice
from tests.test_helpers import (
    create_test_plate_template,
    create_test_transporter,
    wait_for_paused_threads,
    wire_system_map,
)
from tests.test_stop_cancel_and_cascade import (
    _build_join_system,
    _wait_for_shared_rendezvous,
)


def _statuses(runtime: SystemRuntime, eid: str) -> dict[str, str]:
    return {t.name: t.status for t in runtime.list_threads(eid)}


class _FailShakeThenSealDevice(UniversalMockDevice):
    """Shake fails (raises) until cleared; seal always succeeds. Lets a 2-action
    shared method fail its first action, then -- after ABORT_ACTION skips it --
    run its second action cleanly."""

    def __init__(self, name: str, site_names: list[str] | None = None) -> None:
        super().__init__(name, site_names=site_names)
        self.shake_should_fail = True
        self.shake_count = 0
        self.seal_count = 0

    async def shake(self, duration: int, speed: int) -> None:
        self.shake_count += 1
        if self.shake_should_fail:
            raise RuntimeError("Simulated shake failure")
        await super().shake(duration, speed)

    async def seal(self, temperature: int, duration: float) -> None:
        self.seal_count += 1
        await super().seal(temperature, duration)


async def _build_two_action_join_system() -> tuple[SystemRuntime, WorkflowTemplate, _FailShakeThenSealDevice]:
    """An owner runs a TWO-shared-action method (shake then seal); a contributor
    joins. The shake fails, so the owner pauses at the first action while the
    contributor awaits its outcome."""
    device = _FailShakeThenSealDevice("dev1", site_names=["site-1", "site-2"])
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
        system_map, devices={"dev1": device}, pads=["pad1", "pad2"],
    )

    @orca.action(device=pool, inputs=[plate_main, plate_child])
    async def shake_action(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=pool, inputs=[plate_main, plate_child])
    async def seal_action(ctx: ActionContext) -> None:
        await ctx.device().seal(temperature=20, duration=1)

    @orca.method
    async def parent_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake_action
        yield seal_action

    pad1 = system_map.get_location("pad1")
    pad2 = system_map.get_location("pad2")

    @orca.thread(labware=plate_main, start=pad1, end=pad1)
    async def main_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield parent_method

    @orca.thread(labware=plate_child, start=pad2, end=pad2)
    async def child_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        del ctx
        yield orca.join()

    @orca.workflow(name="two_action_join_wf")
    def workflow(wf: WorkflowContext) -> None:
        wf.start(main_thread)
        wf.thread(child_thread)

    event_bus = EventBus()
    builder = SdkToSystemBuilder(
        name="test_system",
        description="",
        labwares=[plate_main, plate_child],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=event_bus,
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), event_bus=event_bus)
    return runtime, workflow, device


async def test_abort_action_does_not_collapse_next_shared_action() -> None:
    """ABORT_ACTION is NOT terminal: it skips the failed shared action and the
    method advances to its NEXT shared action. Only the owner pauses; recovering
    it with ABORT_ACTION must run the second action (seal) and complete, with the
    contributor fanning out to each slot outcome rather than tearing the method
    down. A stale terminal signal that poisoned the method exit would make the
    next action's co-labware wait bail on every participant."""
    runtime, workflow, device = await _build_two_action_join_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        await _wait_for_shared_rendezvous(runtime, record.id)
        assert device.shake_count == 1 and device.seal_count == 0

        # Every participant parks on a shared failure, and the group takes one
        # decision, so recovering them in a loop is idempotent after the first.
        for paused in runtime.get_paused_threads(record.id):
            runtime.recover_thread(record.id, paused.id, RecoveryDecision.ABORT_ACTION)

        final = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert final.status == ExecutionState.COMPLETED
        assert device.seal_count >= 1, (
            "the second shared action must still run after ABORT_ACTION skips "
            "the first; ABORT_ACTION must not tear the method down."
        )
    finally:
        await runtime.shutdown()


async def test_continue_resolves_the_whole_group_and_records_it_once() -> None:
    """One CONTINUE decision carries every participant past the failed shared
    action, and the run records the failure once and the operator's decision
    once. The knowledge base tells operators to decide once and send it, and
    that a shared failure is ONE incident with N paused threads; both of those
    are claims about this path.
    """
    from orca.runtime.incident_store import IncidentCategory

    runtime, workflow, device = await _build_two_action_join_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        await _wait_for_shared_rendezvous(runtime, record.id)
        assert device.shake_count == 1 and device.seal_count == 0

        # _wait_for_shared_rendezvous already established that BOTH plates are
        # parked, which is the contrast worth pinning: two stuck threads, and
        # the failure recorded once because it happened once, on one device.
        paused = runtime.get_paused_threads(record.id)
        failures = await runtime.incidents.list(category=IncidentCategory.ACTION_FAILED)
        assert len(failures) == 1, (
            f"{len(paused)} threads parked on one shared failure, which is "
            f"recorded once; got {[i.thread_id for i in failures]}"
        )
        failed_id = failures[0].id

        # One decision, sent once, resolves the group.
        runtime.recover_thread(record.id, paused[0].id, RecoveryDecision.CONTINUE)

        final = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert final.status == ExecutionState.COMPLETED
        assert device.seal_count >= 1, (
            "the next shared action must run: CONTINUE carries the method on, "
            "it does not tear the rendezvous down"
        )
        continued = await runtime.incidents.list(
            category=IncidentCategory.ACTION_CONTINUED,
        )
        assert len(continued) == 1
        # Re-read and pin by id: SystemIncident is frozen, so a list taken
        # before the CONTINUE could not show what the CONTINUE did.
        still_open = await runtime.incidents.list(category=IncidentCategory.ACTION_FAILED)
        assert [(i.id, i.acknowledged) for i in still_open] == [(failed_id, False)], (
            "continuing past a failure does not resolve it"
        )
    finally:
        await runtime.shutdown()


async def test_retry_op_is_accepted_from_a_contributor_of_a_shared_action() -> None:
    """RETRY_OP applies on a shared action, and any participant may send it.

    The runtime gates RETRY_OP on the GROUP's op-pause rather than the calling
    thread's, precisely so a contributor -- which never drives the device call --
    can feed the decision. Nothing pinned that, and three surfaces now document
    it (the orca request model, a hosted deployment's knowledge base, the operator console blurb), so
    the claim needs a test rather than a comment.
    """
    runtime, workflow, device = await _build_two_action_join_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        await _wait_for_shared_rendezvous(runtime, record.id)
        assert device.shake_count == 1

        paused = runtime.get_paused_threads(record.id)
        contributors = [t for t in paused if t.name.startswith("plate_child")]
        assert contributors, [t.name for t in paused]

        # The device is fixed; the contributor asks for just the failed call again.
        device.shake_should_fail = False
        runtime.recover_thread(record.id, contributors[0].id, RecoveryDecision.RETRY_OP)

        final = await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert final.status == ExecutionState.COMPLETED
        assert device.shake_count == 2, (
            "RETRY_OP re-ran only the failed device call"
        )
        assert device.seal_count >= 1, "the action body resumed and the method finished"
    finally:
        await runtime.shutdown()


async def test_abort_thread_collapses_contributor_parked_at_resolution(monkeypatch) -> None:
    """Pre-binding: the owner can pause at action RESOLUTION (before it binds
    ``_current_action``), e.g. an ``ActionReservationTimeoutError`` /
    ``UnresolvableDeadlockError``. In that window the contributor is parked in
    ``wait_for_current_action`` with no bound action to await. The owner
    publishes its terminal decision on the slot outcome when it exits
    resolution, so a single ABORT_THREAD on the owner reaches the parked
    contributor (via ``SharedRendezvousResolved``) and it lands ABORTED, not
    stranded PAUSED with the execution RUNNING forever."""
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

    runtime, workflow, device = await _build_join_system()

    async def _fail_resolution(
        self: ExecutingMethod,
        thread_id: str,
        current_location: Location,
        action_resolver: DynamicResourceActionResolver,
        requesting_labware: LabwareInstance | None = None,
        status_sink: IActionReservationStatusSink | None = None,
    ) -> ExecutableLocationAction:
        raise ActionReservationTimeoutError(thread_id, 0.0, ["dev1"], "rejected")

    monkeypatch.setattr(ExecutingMethod, "resolve_current_action", _fail_resolution)

    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        # Only the owner pauses (at resolution); the contributor is parked in
        # wait_for_current_action, which is NOT a paused state.
        await wait_for_paused_threads(runtime, record.id)
        paused = runtime.get_paused_threads(record.id)
        assert len(paused) == 1, (
            f"only the owner pauses at resolution; the contributor is parked in "
            f"wait_for_current_action. got {_statuses(runtime, record.id)}"
        )
        owner = paused[0]

        runtime.recover_thread(record.id, owner.id, RecoveryDecision.ABORT_THREAD)

        await asyncio.wait_for(runtime.wait(record.id), timeout=15.0)
        assert not runtime.get_paused_threads(record.id), (
            f"ABORT_THREAD must tear the rendezvous down with no zombie PAUSED thread; "
            f"got {[t.name for t in runtime.get_paused_threads(record.id)]}."
        )
        owner_now = next(t for t in runtime.list_threads(record.id) if t.id == owner.id)
        assert owner_now.status == "ABORTED", (
            f"the recovered owner must abort cleanly, not zombie-park; got {owner_now.status}."
        )
    finally:
        try:
            await asyncio.wait_for(runtime.wait(record.id), timeout=8.0)
        except Exception:
            await runtime.abort_execution(record.id)
        await runtime.shutdown()


async def test_abort_execution_mid_rendezvous_terminates_all() -> None:
    """SAFETY NET: abort_execution while the owner is paused on a shared failure
    drives the whole execution terminal (ABORTED) and leaves no thread in a
    non-terminal limbo that would wedge a re-run."""
    runtime, workflow, device = await _build_join_system()
    await runtime.start()
    record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.PURE_SIM)
    try:
        await _wait_for_shared_rendezvous(runtime, record.id)

        await runtime.abort_execution(record.id)
        final = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        assert final.status == ExecutionState.ABORTED
    finally:
        await runtime.shutdown()
