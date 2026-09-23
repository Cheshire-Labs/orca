"""An abnormally-dead receiver with undelivered contributions quarantines its
slot instead of poisoning it.

Pre-fix, a receiver that reached ABORTED/STOPPED while its slot still held
undelivered methods left a poisoned backlog: ``has_active_thread()`` treats
the dead receiver as absent, so the next contributor minted a fresh receiver
into methods bound to the dead thread (the DoubleAssignment class), and the
stuck owners parked at AWAITING_CO_THREADS unbounded. The fix quarantines the
slot (``orphaned``), pauses the in-scope threads, records an
ORPHANED_BACKLOG incident, gates closure evaluation and fresh mints, and
disposes of the backlog only on the operator's execution-level resume
(accept-partial): owners exit via METHOD_EXIT and continue their journeys.

Topology mirrors the premature-close probe (feeder -> mid -> pool, pool
SHARED_ACROSS_GROUPS): two groups feed one shared pool receiver; the receiver
serves contribution #1, is held in its own generator body while #2 is
delivered, and is then cooperatively stopped, dying STOPPED with #2
undelivered.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.events.event_bus import EventBus as CoreEventBus
from orca.events.execution_context import WorkflowExecutionContext
from orca.resource_models.labware_state import SLOT_CLOSED_SENTINEL, LabwareSlot
from orca.runtime.incident_store import IncidentCategory, RecoveryAction
from orca.workflow_models.method import ExecutingMethod, MethodInstance
from orca.workflow_models.status_manager import StatusManager
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.submission import BatchMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import ActionTemplate, IMethodTemplate
from orca.workflow_models.thread_context import ThreadContext

from tests.closure_e2e_scaffold import (
    TERMINAL_STATUSES,
    build_closure_scaffold,
    feeder_group,
    finish_closure_system,
    pool_slot,
    wait_for_boot,
)
from tests.test_helpers import wait_until


class _Recorder:
    def __init__(self) -> None:
        self.use_pool_runs = 0
        self.use_pool_b_runs = 0


class _PoolGate:
    """Holds the pool receiver in its own generator body between joins."""

    def __init__(self) -> None:
        self.served_one = asyncio.Event()
        self.hold = asyncio.Event()


class _BodyGate:
    """Holds use_pool_b's ACTION BODY so the drain sees a running user task."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.hold = asyncio.Event()


class _FeederGate:
    """First feeder runs free; later feeders wait for staged releases."""

    def __init__(self) -> None:
        self.may_start = asyncio.Event()
        self._count = 0

    def claim(self) -> int:
        self._count += 1
        return self._count


async def _build_system(
    recorder: _Recorder, pool_gate: _PoolGate, feeder_gate: _FeederGate,
    b_body_gate: _BodyGate | None = None,
) -> tuple[ISystem, WorkflowTemplate, EventBus]:
    scaffold = await build_closure_scaffold()
    feeder = scaffold.feeder
    mid = scaffold.mid
    pool = scaffold.pool

    @orca.action(device=scaffold.feeder_station_pool, inputs=[feeder, mid])
    async def make_mid(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    @orca.action(device=scaffold.mid_station_pool, inputs=[mid, pool])
    async def use_pool(ctx: ActionContext) -> None:
        recorder.use_pool_runs += 1
        await ctx.device().shake(duration=1, speed=500)

    # Contribution #2 rendezvouses at a DIFFERENT station than #1, so its owner
    # genuinely wedges instead of completing via labware-already-present.
    @orca.action(device=scaffold.pool_station_pool, inputs=[mid, pool])
    async def use_pool_b(ctx: ActionContext) -> None:
        recorder.use_pool_b_runs += 1
        if b_body_gate is not None:
            b_body_gate.started.set()
            await b_body_gate.hold.wait()
        await ctx.device().shake(duration=1, speed=500)

    async def _make_mid_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield make_mid
    make_mid_method = MethodTemplate("make_mid_method", func=_make_mid_method)

    async def _use_pool_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield use_pool
    use_pool_method = MethodTemplate("use_pool_method", func=_use_pool_method)

    async def _use_pool_b_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield use_pool_b
    use_pool_b_method = MethodTemplate("use_pool_b_method", func=_use_pool_b_method)

    async def _feeder_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        if feeder_gate.claim() >= 2:
            await feeder_gate.may_start.wait()
        yield make_mid_method
    feeder_thread = ThreadTemplate(
        labware_template=feeder,
        start=scaffold.feeder_pad,
        end=scaffold.waste,
        func=_feeder_thread,
        contributes_to=["mid"],
    )

    mid_claims = {"count": 0}

    async def _mid_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join(allows=[make_mid_method])
        mid_claims["count"] += 1
        # First mid converges at mid_station; the second at pool_station.
        yield use_pool_method if mid_claims["count"] == 1 else use_pool_b_method
    mid_thread = ThreadTemplate(
        labware_template=mid,
        start=scaffold.mid_pad,
        end=scaffold.waste,
        func=_mid_thread,
        contributes_to=[],
    )

    async def _pool_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        first = True
        while ctx.has_more_work():
            yield orca.join(allows=[use_pool_method, use_pool_b_method])
            if first:
                first = False
                pool_gate.served_one.set()
                # Held in USER code between joins: contribution #2 arrives and
                # queues while the receiver is neither awaiting nor drained.
                await pool_gate.hold.wait()
    pool_thread = ThreadTemplate(
        labware_template=pool,
        start=scaffold.pool_pad,
        end=scaffold.pool_pad,
        func=_pool_thread,
    )

    return await finish_closure_system(
        scaffold,
        workflow_name="orphaned_backlog_demo",
        system_name="orphaned_backlog_system",
        feeder_thread=feeder_thread,
        mid_thread=mid_thread,
        pool_thread=pool_thread,
    )


@pytest.mark.asyncio
async def test_dead_receiver_quarantines_then_resume_accepts_partial() -> None:
    recorder = _Recorder()
    pool_gate = _PoolGate()
    feeder_gate = _FeederGate()
    system, workflow, event_bus = await _build_system(recorder, pool_gate, feeder_gate)
    runtime = SystemRuntime(system, event_bus=event_bus)
    await runtime.start()
    try:
        sub = await runtime.submit(
            workflow,
            groups=[feeder_group("grp-1"), feeder_group("grp-2")],
            batch_mode=BatchMode.STANDALONE,
            mode=WorkflowRunMode.PURE_SIM,
        )
        eid = sub.execution_id
        await wait_for_boot(runtime, eid)

        # Contribution #1 served; receiver held in its generator body.
        await asyncio.wait_for(pool_gate.served_one.wait(), timeout=40.0)
        assert recorder.use_pool_runs == 1
        slot = pool_slot(runtime, eid)
        assert slot is not None

        # Contribution #2 delivered while the receiver is held: it queues,
        # bound to the (soon dead) receiver.
        feeder_gate.may_start.set()
        await wait_until(
            lambda: slot.queue.qsize() >= 1,
            timeout=40.0,
            message="contribution #2 never queued behind the held receiver",
        )

        wf = runtime._executions[eid].executing_workflow
        assert wf is not None
        pool_exec = next(
            t for t in wf.threads
            if t.thread_instance.thread_template is not None
            and t.thread_instance.thread_template.name == "pool"
        )

        # Kill the receiver: cooperative stop, then release the user-code hold
        # so it unwinds. It dies STOPPED with #2 undelivered.
        pool_exec.stop()
        pool_gate.hold.set()
        await wait_until(
            lambda: pool_exec.status.name == "STOPPED",
            timeout=40.0,
            message="receiver never reached STOPPED",
        )

        # --- Quarantine (red on main: no orphaned attr / fresh mint instead) ---
        assert slot.orphaned, "dead receiver with backlog must quarantine its slot"
        assert not slot.is_closed, "quarantined slot must not close"
        assert slot.active_thread is pool_exec, (
            "quarantine must not clear the dead receiver; the drain does that"
        )

        incidents = await runtime.incidents.list()
        orphan_incidents = [
            i for i in incidents if i.category is IncidentCategory.ORPHANED_BACKLOG
        ]
        assert len(orphan_incidents) == 1, (
            f"expected exactly one ORPHANED_BACKLOG incident, got {incidents}"
        )
        assert orphan_incidents[0].recovery_action is RecoveryAction.RESUME_EXECUTION

        # The stuck owner (mid-2, parked at co-labware for the dead receiver's
        # labware) is pause-requested and lands PAUSED, not wedged invisibly.
        await wait_until(
            lambda: any(t.status == "PAUSED" for t in runtime.list_threads(eid)),
            timeout=40.0,
            message="no in-scope thread reached PAUSED after quarantine",
        )
        # No fresh receiver was minted into the quarantined backlog.
        pool_thread_count = sum(
            1 for t in wf.threads
            if t.thread_instance.thread_template is not None
            and t.thread_instance.thread_template.name == "pool"
        )
        assert pool_thread_count == 1, "fresh receiver minted into quarantine"

        # Never-rebind extends to operator spawn-recovery.
        with pytest.raises(RuntimeError, match="orphaned backlog"):
            await runtime.spawn_thread_in_execution(eid, "pool")
        # JOIN_EXISTING refused while quarantined; checked once the feeders
        # have cleared feeder_pad so the occupancy guard cannot fire first.
        from orca.runtime.runtime_interface import (
            SubmissionBlockedByOrphanedBacklogError,
        )
        await wait_until(
            lambda: sum(
                1 for t in runtime.list_threads(eid)
                if t.name.startswith("feeder") and t.status in TERMINAL_STATUSES
            ) == 2,
            timeout=40.0,
            message="feeders never reached terminal",
        )
        with pytest.raises(SubmissionBlockedByOrphanedBacklogError):
            await runtime.submit(
                workflow,
                groups=[feeder_group("grp-3")],
                batch_mode=BatchMode.JOIN_EXISTING,
                mode=WorkflowRunMode.PURE_SIM,
            )
        # A bare resume_all_threads (the recoverable-timeout path) must NOT
        # dispose of the quarantine; only the execution-level resume does.
        runtime.resume_all_threads(eid)
        await wait_until(
            lambda: all(t.status != "PAUSED" for t in runtime.list_threads(eid)),
            timeout=40.0,
            message="resume_all_threads never resumed the paused threads",
        )
        assert slot.orphaned, "resume_all_threads must not drain quarantine"
        assert slot.undelivered_count() == 1

        # --- Accept-partial: execution-level resume drains and unblocks ------
        runtime.resume_execution(eid)
        await wait_until(
            lambda: all(
                t.status in TERMINAL_STATUSES for t in runtime.list_threads(eid)
            ) and bool(runtime.list_threads(eid)),
            timeout=40.0,
            message=(
                "threads never quiesced after accept-partial resume: "
                f"{[(t.name, t.status) for t in runtime.list_threads(eid)]}"
            ),
        )
        assert recorder.use_pool_runs == 1
        assert recorder.use_pool_b_runs == 0, (
            "the orphaned contribution must be abandoned, not executed"
        )
        # End-of-run teardown (cleanup of parked threads) must not mint
        # further ORPHANED_BACKLOG incidents on top of the real one.
        incidents_after = await runtime.incidents.list()
        assert sum(
            1 for i in incidents_after
            if i.category is IncidentCategory.ORPHANED_BACKLOG
        ) == 1, "teardown minted spurious ORPHANED_BACKLOG incidents"
        assert not slot.orphaned, "drain must clear the quarantine"
        assert slot.is_closed, "drain must close the slot"
        assert slot.undelivered_count() == 0, "backlog must be gone after the drain"
        statuses = {t.name: t.status for t in runtime.list_threads(eid)}
        assert sum(1 for s in statuses.values() if s == "STOPPED") == 1, (
            f"only the dead receiver is STOPPED; got {statuses}"
        )
    finally:
        pool_gate.hold.set()
        feeder_gate.may_start.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_no_backlog_death_is_not_an_orphan() -> None:
    """A receiver dying with nothing owed mints no quarantine; the next
    contribution legally mints a fresh receiver (post-death fresh mint)."""
    recorder = _Recorder()
    pool_gate = _PoolGate()
    feeder_gate = _FeederGate()
    system, workflow, event_bus = await _build_system(recorder, pool_gate, feeder_gate)
    runtime = SystemRuntime(system, event_bus=event_bus)
    await runtime.start()
    try:
        sub = await runtime.submit(
            workflow,
            groups=[feeder_group("grp-1"), feeder_group("grp-2")],
            batch_mode=BatchMode.STANDALONE,
            mode=WorkflowRunMode.PURE_SIM,
        )
        eid = sub.execution_id
        await wait_for_boot(runtime, eid)
        await asyncio.wait_for(pool_gate.served_one.wait(), timeout=40.0)
        slot = pool_slot(runtime, eid)
        assert slot is not None

        # Kill the receiver BEFORE contribution #2 exists: empty queue, no
        # in-flight method (the receiver is between joins in user code).
        wf = runtime._executions[eid].executing_workflow
        assert wf is not None
        pool_exec = next(
            t for t in wf.threads
            if t.thread_instance.thread_template is not None
            and t.thread_instance.thread_template.name == "pool"
        )
        pool_exec.stop()
        pool_gate.hold.set()
        await wait_until(
            lambda: pool_exec.status.name == "STOPPED",
            timeout=40.0,
            message="receiver never reached STOPPED",
        )
        assert not slot.orphaned, "no backlog -> no quarantine"

        # The late contribution mints a legitimately fresh receiver.
        feeder_gate.may_start.set()
        await wait_until(
            lambda: recorder.use_pool_b_runs == 1,
            timeout=40.0,
            message="fresh receiver never served the post-death contribution",
        )
        await wait_until(
            lambda: bool(runtime.list_threads(eid)) and all(
                t.status in TERMINAL_STATUSES for t in runtime.list_threads(eid)
            ),
            timeout=40.0,
            message="execution never quiesced",
        )
        incidents = await runtime.incidents.list()
        assert not any(
            i.category is IncidentCategory.ORPHANED_BACKLOG for i in incidents
        ), "spurious ORPHANED_BACKLOG for a no-backlog death"
    finally:
        pool_gate.hold.set()
        feeder_gate.may_start.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_in_flight_method_quarantines_and_executing_action_completes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A receiver dying mid-participation flags its in-flight method; the
    drain leaves a method whose action body is executing to complete."""
    caplog.set_level(logging.INFO, logger="orca")
    recorder = _Recorder()
    pool_gate = _PoolGate()
    feeder_gate = _FeederGate()
    b_gate = _BodyGate()
    system, workflow, event_bus = await _build_system(
        recorder, pool_gate, feeder_gate, b_body_gate=b_gate,
    )
    runtime = SystemRuntime(system, event_bus=event_bus)
    await runtime.start()
    try:
        sub = await runtime.submit(
            workflow,
            groups=[feeder_group("grp-1"), feeder_group("grp-2")],
            batch_mode=BatchMode.STANDALONE,
            mode=WorkflowRunMode.PURE_SIM,
        )
        eid = sub.execution_id
        await wait_for_boot(runtime, eid)
        await asyncio.wait_for(pool_gate.served_one.wait(), timeout=40.0)
        slot = pool_slot(runtime, eid)
        assert slot is not None

        # Let the receiver dequeue #2 and rendezvous; the action body gates.
        feeder_gate.may_start.set()
        pool_gate.hold.set()
        await asyncio.wait_for(b_gate.started.wait(), timeout=40.0)

        wf = runtime._executions[eid].executing_workflow
        assert wf is not None
        pool_exec = next(
            t for t in wf.threads
            if t.thread_instance.thread_template is not None
            and t.thread_instance.thread_template.name == "pool"
        )
        # The receiver follows the executing action; stop it mid-participation.
        pool_exec.stop()
        await wait_until(
            lambda: pool_exec.status.name == "STOPPED",
            timeout=40.0,
            message="receiver never reached STOPPED mid-participation",
        )
        assert slot.orphaned, "in-flight contribution must quarantine the slot"
        incidents = await runtime.incidents.list()
        orphan = [
            i for i in incidents
            if i.category is IncidentCategory.ORPHANED_BACKLOG
        ]
        assert len(orphan) == 1
        assert "use_pool_b_method" in orphan[0].message

        # Accept-partial: the drain must SKIP the mid-execution method (its
        # user task is running on the 'device'), not cancel it.
        runtime.resume_execution(eid)
        await wait_until(
            lambda: "mid-execution" in caplog.text,
            timeout=40.0,
            message="drain never logged the executing-method skip",
        )
        b_gate.hold.set()
        await wait_until(
            lambda: bool(runtime.list_threads(eid)) and all(
                t.status in TERMINAL_STATUSES for t in runtime.list_threads(eid)
            ),
            timeout=40.0,
            message="execution never quiesced after accept-partial",
        )
        assert recorder.use_pool_b_runs == 1, (
            "the executing action must complete, not be cancelled"
        )
        assert not slot.orphaned and slot.is_closed
    finally:
        pool_gate.hold.set()
        feeder_gate.may_start.set()
        b_gate.hold.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_double_resume_is_idempotent() -> None:
    """Two rapid execution-level resumes: one drain wins, no clobber."""
    recorder = _Recorder()
    pool_gate = _PoolGate()
    feeder_gate = _FeederGate()
    system, workflow, event_bus = await _build_system(recorder, pool_gate, feeder_gate)
    runtime = SystemRuntime(system, event_bus=event_bus)
    await runtime.start()
    try:
        sub = await runtime.submit(
            workflow,
            groups=[feeder_group("grp-1"), feeder_group("grp-2")],
            batch_mode=BatchMode.STANDALONE,
            mode=WorkflowRunMode.PURE_SIM,
        )
        eid = sub.execution_id
        await wait_for_boot(runtime, eid)
        await asyncio.wait_for(pool_gate.served_one.wait(), timeout=40.0)
        slot = pool_slot(runtime, eid)
        assert slot is not None
        feeder_gate.may_start.set()
        await wait_until(
            lambda: slot.queue.qsize() >= 1,
            timeout=40.0,
            message="contribution #2 never queued",
        )
        wf = runtime._executions[eid].executing_workflow
        assert wf is not None
        pool_exec = next(
            t for t in wf.threads
            if t.thread_instance.thread_template is not None
            and t.thread_instance.thread_template.name == "pool"
        )
        pool_exec.stop()
        pool_gate.hold.set()
        await wait_until(
            lambda: pool_exec.status.name == "STOPPED",
            timeout=40.0,
            message="receiver never reached STOPPED",
        )
        assert slot.orphaned

        runtime.resume_execution(eid)
        runtime.resume_execution(eid)
        await wait_until(
            lambda: bool(runtime.list_threads(eid)) and all(
                t.status in TERMINAL_STATUSES for t in runtime.list_threads(eid)
            ),
            timeout=40.0,
            message="execution never quiesced after double resume",
        )
        assert not slot.orphaned and slot.is_closed
        assert slot.active_thread is None, "stale drain clobbered the slot"
        assert recorder.use_pool_b_runs == 0
        pool_thread_count = sum(
            1 for t in wf.threads
            if t.thread_instance.thread_template is not None
            and t.thread_instance.thread_template.name == "pool"
        )
        assert pool_thread_count == 1, "double drain minted a duplicate receiver"
    finally:
        pool_gate.hold.set()
        feeder_gate.may_start.set()
        await runtime.shutdown()


class TestOrphanedBacklogWireShapes:
    def test_incident_detail_round_trips(self) -> None:
        from orca.runtime.db.incident_mapping import detail_to_json, json_to_detail
        from orca.system.reservation_manager.errors import OrphanedBacklogContext

        context = OrphanedBacklogContext(
            slot_key="pool:*:sub-1",
            labware_template_name="pool",
            receiver_thread_id="t-1",
            receiver_thread_name="pool-abc",
            receiver_status="STOPPED",
            undelivered_count=2,
            in_flight_method_name="use_pool_b_method",
            pause_requested_thread_ids=("t-2", "t-3"),
        )
        assert json_to_detail(
            IncidentCategory.ORPHANED_BACKLOG, detail_to_json(context)
        ) == context

    def test_join_refusal_wire_code(self) -> None:
        from orca.operations.submission import (
            _raise_submission_blocked_by_orphaned_backlog,
        )
        from orca.runtime.runtime_interface import (
            SubmissionBlockedByOrphanedBacklogError,
        )

        err = _raise_submission_blocked_by_orphaned_backlog(
            SubmissionBlockedByOrphanedBacklogError(
                blocking_execution_id="e-1", blocking_workflow_name="wf",
            )
        )
        assert err.wire_code == "submission_blocked_by_orphaned_backlog"
        assert err.status_code == 409
        assert err.extras is not None
        assert err.extras["blocking_execution_id"] == "e-1"
        assert err.extras["blocking_workflow_name"] == "wf"


def _make_method(name: str, done: bool = False) -> ExecutingMethod:
    event_bus = CoreEventBus()
    method = ExecutingMethod(
        MethodInstance(name),
        event_bus,
        StatusManager(event_bus),
        WorkflowExecutionContext(execution_id="wf-1", workflow_name="t"),
    )
    if done:
        method.completed.set()
    return method


class TestLabwareSlotQuarantineFields:
    def test_orphaned_defaults(self) -> None:
        slot = LabwareSlot(slot_key="pool:*:*", labware_template_name="pool")
        assert slot.orphaned is False
        assert slot.orphaned_in_flight is None

    @pytest.mark.asyncio
    async def test_await_next_method_returns_none_on_orphaned_slot(self) -> None:
        # A late-arriving consumer (an operator-injected receiver) must never
        # dequeue a quarantined backlog.
        slot = LabwareSlot(slot_key="pool:*:*", labware_template_name="pool")
        slot.queue.put_nowait(_make_method("backlog"))
        slot.orphaned = True
        assert await slot.await_next_method(asyncio.Event()) is None
        assert slot.queue.qsize() == 1, "the backlog must stay untouched"

    @pytest.mark.asyncio
    async def test_undelivered_count_skips_sentinel_and_completed(self) -> None:
        slot = LabwareSlot(slot_key="pool:*:*", labware_template_name="pool")
        live_1 = _make_method("live-1")
        live_2 = _make_method("live-2")
        slot.queue.put_nowait(live_1)
        slot.queue.put_nowait(SLOT_CLOSED_SENTINEL)
        slot.queue.put_nowait(_make_method("done-q", done=True))
        slot.pending.append(live_2)
        slot.pending.append(_make_method("done-p", done=True))

        assert slot.undelivered_count() == 2

        # Inspection is non-destructive and order-preserving.
        assert slot.queue.qsize() == 3
        assert slot.queue.get_nowait() is live_1
        assert slot.queue.get_nowait() is SLOT_CLOSED_SENTINEL
        assert len(slot.pending) == 2
        assert slot.pending[0] is live_2
