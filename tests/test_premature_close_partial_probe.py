"""Regression: a LOOPING worker-quiesced receiver must NOT be closed while a live
non-parked thread still owes it a contribution.

Pre-fix, the worker-quiescence branch excluded any receiver of ANY open slot from
every slot's worker set (bare identity), so a thread that was both an owner and a
receiver was invisible to the close decision. Here that closed the pool while mid-2
(alive, executing, owing a contribution) was mid-journey: the pool finalized on a
PARTIAL fill and the stranded delivery wedged the execution with
DoubleAssignmentError. The fix discriminates parked-vs-executing: only a receiver
PARKED on an open slot is excluded; an executing thread counts as a worker for
every slot. This test pins the fixed behavior end to end (slot stays open, both
contributions served, finalize on the full fill, clean quiesce).

This is the general-mechanism counterpart to the SMC ``tips_read`` premature-close
bug (the worker-quiescence receiver exclusion in
``ExecutingWorkflow._evaluate_slot_closures``). The SMC example never hits this
looping variant because its only looping receiver with a post-loop downstream
action (``final_plate``) is FEEDER-governed and therefore safe; its worker-quiesced
looping receivers (the deck reservoirs) have no post-loop action. This test builds
the shape SMC lacks.

Topology (mirrors ``plate_1 -> neut_plate -> tips_read`` but makes the pool loop):

    feeder (start, contributes_to=[mid])
        -> mid (auto-spawn receiver; contributes_to=[] so it is NOT a feeder of pool)
             owner of use_pool_method, whose input is pool
        -> pool (auto-spawn, SHARED_ACROSS_GROUPS, in NO contributes_to chain ->
                 WORKER-QUIESCED governs its slot)
             body: while ctx.has_more_work(): yield join(use_pool_method); then finalize

Two groups in one submission feed ONE shared pool, so the pool legitimately owes 2
``use_pool_method`` contributions. The test gates the interleaving so that:

  1. group-1's mid delivers contribution #1; pool spawns, consumes it, parks in its
     has_more_work loop awaiting #2.
  2. group-2's mid has joined and is gated in user code BEFORE it yields
     use_pool_method (still owing #2). It is a receiver of its own mid slot AND
     an executing thread -- the exact both-roles shape the old predicate missed.
  3. group-2's feeder completes. Its terminal fires _evaluate_slot_closures.
     mid-2 is executing (not parked in await_next_method), so it counts as a
     worker for every slot and the pool must STAY OPEN. Pre-fix, the bare
     identity exclusion closed the pool here.
  4. mid-2 is released, yields use_pool_method, and its contribution is served
     by the still-parked pool receiver: full fill.
  5. once every in-scope thread is parked or done, the close fires, the pool
     exits its loop, finalizes on 2 of 2 contributions, and the execution
     quiesces cleanly.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
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
    """Shared observation state across the workflow's actions and the test."""

    def __init__(self) -> None:
        self.use_pool_runs = 0
        # use_pool_runs snapshot at each finalize invocation (one per pool instance).
        self.finalize_snapshots: list[int] = []


class _SecondMidGate:
    """First mid to arrive passes free; the second parks until released.

    Mirrors the two-plate convergence: mid-1 delivers contribution #1 immediately,
    mid-2 is frozen after its join (still owing contribution #2) so it is a live
    receiver of an open slot at the moment the pool is (wrongly) evaluated for close.
    """

    def __init__(self) -> None:
        self.second_reached = asyncio.Event()
        self.release = asyncio.Event()
        self._count = 0

    async def wait_if_second(self) -> None:
        self._count += 1
        if self._count == 2:
            self.second_reached.set()
            await self.release.wait()


class _FeederGate:
    """Sequences the two feeder INSTANCES (one template, two groups) by claim order.

    The first feeder to claim runs free. The second waits on ``may_start`` (so the
    first mid delivers before the second mid is spawned, fixing the mid_gate order),
    then after it spawns its mid it waits on ``may_finish`` so its COMPLETION -- the
    terminal that fires the wrongful close -- is driven by the test.
    """

    def __init__(self) -> None:
        self.may_start = asyncio.Event()
        self.may_finish = asyncio.Event()
        self._count = 0

    def claim(self) -> int:
        self._count += 1
        return self._count


async def _build_system(
    recorder: _Recorder, mid_gate: _SecondMidGate, feeder_gate: _FeederGate,
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

    @orca.action(device=scaffold.pool_station_pool, inputs=[pool])
    async def finalize_pool(ctx: ActionContext) -> None:
        recorder.finalize_snapshots.append(recorder.use_pool_runs)
        await ctx.device().shake(duration=1, speed=200)

    async def _make_mid_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield make_mid
    make_mid_method = MethodTemplate("make_mid_method", func=_make_mid_method)

    async def _use_pool_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield use_pool
    use_pool_method = MethodTemplate("use_pool_method", func=_use_pool_method)

    async def _finalize_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield finalize_pool
    finalize_method = MethodTemplate("finalize_method", func=_finalize_method)

    async def _feeder_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        idx = feeder_gate.claim()
        if idx >= 2:
            await feeder_gate.may_start.wait()
        yield make_mid_method
        if idx >= 2:
            await feeder_gate.may_finish.wait()
    feeder_thread = ThreadTemplate(
        labware_template=feeder,
        start=scaffold.feeder_pad,
        end=scaffold.waste,
        func=_feeder_thread,
        contributes_to=["mid"],
    )

    async def _mid_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join(allows=[make_mid_method])
        await mid_gate.wait_if_second()
        yield use_pool_method
    mid_thread = ThreadTemplate(
        labware_template=mid,
        start=scaffold.mid_pad,
        end=scaffold.waste,
        func=_mid_thread,
        contributes_to=[],
    )

    async def _pool_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        while ctx.has_more_work():
            yield orca.join(allows=[use_pool_method])
        yield finalize_method
    pool_thread = ThreadTemplate(
        labware_template=pool,
        start=scaffold.pool_pad,
        end=scaffold.pool_pad,
        func=_pool_thread,
    )

    return await finish_closure_system(
        scaffold,
        workflow_name="premature_close_partial_demo",
        system_name="premature_close_partial_system",
        feeder_thread=feeder_thread,
        mid_thread=mid_thread,
        pool_thread=pool_thread,
    )


@pytest.mark.asyncio
async def test_no_premature_close_while_live_owner_owes() -> None:
    recorder = _Recorder()
    mid_gate = _SecondMidGate()
    feeder_gate = _FeederGate()
    system, workflow, event_bus = await _build_system(recorder, mid_gate, feeder_gate)
    runtime = SystemRuntime(system, event_bus=event_bus)
    await runtime.start()
    try:
        sub = await runtime.submit(
            workflow,
            groups=[
                feeder_group("grp-1"),
                feeder_group("grp-2"),
            ],
            batch_mode=BatchMode.STANDALONE,
            mode=WorkflowRunMode.PURE_SIM,
        )
        eid = sub.execution_id
        await wait_for_boot(runtime, eid)

        # Step 1: the first mid delivers contribution #1; pool spawns, consumes it,
        # and parks in its has_more_work loop awaiting #2.
        await wait_until(
            lambda: recorder.use_pool_runs >= 1 and pool_slot(runtime, eid) is not None,
            timeout=40.0,
            message="pool never consumed the first contribution",
        )
        slot = pool_slot(runtime, eid)
        assert slot is not None
        assert not slot.is_closed, "pool closed before the trigger; setup race"

        # Step 2: let the second feeder run so it spawns mid-2. mid-2 joins and then
        # parks at the gate BEFORE yielding use_pool_method (still owing #2).
        feeder_gate.may_start.set()
        await asyncio.wait_for(mid_gate.second_reached.wait(), timeout=40.0)
        for _ in range(20):
            await asyncio.sleep(0)

        assert not slot.is_closed, (
            "pool must still be open while the second mid is parked owing a contribution"
        )

        # Step 3: the feeder terminal fires closure evaluation synchronously;
        # mid-2 is EXECUTING (gated in user code, not parked) -> pool stays open.
        feeder_gate.may_finish.set()
        await wait_until(
            lambda: sum(
                1 for t in runtime.list_threads(eid)
                if t.name.startswith("feeder") and t.status in TERMINAL_STATUSES
            ) == 2,
            timeout=40.0,
            message="grp-2 feeder never reached terminal",
        )
        for _ in range(20):
            await asyncio.sleep(0)
        slot = pool_slot(runtime, eid)
        assert slot is not None
        assert not slot.is_closed, (
            "pool slot closed while a live executing thread (mid-2) still owes "
            "a contribution: the worker-quiescence category error is back"
        )

        # Step 4: release mid-2. Its contribution is delivered into the OPEN
        # slot and served by the parked pool receiver.
        mid_gate.release.set()
        await wait_until(
            lambda: recorder.use_pool_runs == 2,
            timeout=40.0,
            message="the second contribution was never served",
        )

        # Step 5: everything parked or done -> close fires, pool finalizes on
        # the FULL fill, execution quiesces.
        statuses: dict[str, str] = {}
        deadline = asyncio.get_event_loop().time() + 40.0
        while asyncio.get_event_loop().time() < deadline:
            statuses = {t.name: t.status for t in runtime.list_threads(eid)}
            if statuses and all(s in TERMINAL_STATUSES for s in statuses.values()):
                break
            await asyncio.sleep(0.05)
    finally:
        mid_gate.release.set()
        feeder_gate.may_start.set()
        feeder_gate.may_finish.set()
        await runtime.shutdown()

    # --- Regression: the premature close is fixed -------------------------------

    assert recorder.use_pool_runs == 2, (
        f"both owed contributions must execute, got use_pool_runs={recorder.use_pool_runs}"
    )
    assert recorder.finalize_snapshots, "pool never reached its post-loop finalize"
    assert recorder.finalize_snapshots[0] == 2, (
        "the finalize must run on the FULL fill (2 of 2 contributions), got "
        f"fill={recorder.finalize_snapshots[0]}"
    )
    assert statuses and all(s in TERMINAL_STATUSES for s in statuses.values()), (
        f"the execution must quiesce cleanly; statuses={statuses}"
    )
    assert slot.is_closed, (
        "the pool slot must close once every in-scope thread is parked or done"
    )
