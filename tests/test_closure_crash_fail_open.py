"""Fail-open pin: a thread crash clears recorded join waiters, bounded.

``LabwareSlot.awaiting_threads`` records a receiver's identity while it waits
empty-handed in ``await_next_method`` and removes it in a ``finally``. This
test pins that the identity comes off when the thread that was going to feed
it dies: an ABORT-policy action raise escapes the spawned thread task, the
crashed thread lands terminal, and the slot close that follows must unwind the
parked receiver out of ``await_next_method``. If the identity latched instead,
the registry would carry a phantom waiter into later evaluations and the
awaiting-join surface would lie to operators. The generator-raise route cannot
pin this (a raise between methods routes to the PAUSE recovery fallback and
never escapes the task); only an ABORT-policy action does.

Topology (cribbed from ``test_premature_close_partial_probe``): two feeder
groups feed one SHARED_ACROSS_GROUPS pool. The first mid delivers a
contribution so the pool receiver loops back and is RECORDED awaiting the
second; the second mid crashes instead of delivering. Pinned: execution
reaches FAILED naming the crash, the execution task finishes (no hang), and
``awaiting_threads`` drains to empty.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

import orca.orca as orca
from orca.runtime.execution import ExecutionPhase
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.submission import BatchMode
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.events import EventBus
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.method_template import ActionTemplate, IMethodTemplate
from orca.workflow_models.status_enums import FailurePolicy
from orca.workflow_models.thread_context import ThreadContext

from tests.closure_e2e_scaffold import (
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


class _MidRouter:
    """First mid to claim delivers the pool contribution; the second parks at
    the gate until released, then yields the crashing method."""

    def __init__(self) -> None:
        self.crasher_staged = asyncio.Event()
        self.crash_release = asyncio.Event()
        self._count = 0

    def claim(self) -> int:
        self._count += 1
        return self._count


async def _build_system(
    recorder: _Recorder, router: _MidRouter,
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

    @orca.action(
        device=scaffold.mid_station_pool, inputs=[mid],
        failure_policy=FailurePolicy.ABORT)
    async def crash_op(ctx: ActionContext) -> None:
        raise RuntimeError("deliberate crash: awaiting-join fail-open probe")

    async def _make_mid_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield make_mid
    make_mid_method = MethodTemplate("make_mid_method", func=_make_mid_method)

    async def _use_pool_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield use_pool
    use_pool_method = MethodTemplate("use_pool_method", func=_use_pool_method)

    async def _crash_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield crash_op
    crash_method = MethodTemplate("crash_method", func=_crash_method)

    async def _feeder_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield make_mid_method
    feeder_thread = ThreadTemplate(
        labware_template=feeder,
        start=scaffold.feeder_pad,
        end=scaffold.waste,
        func=_feeder_thread,
        contributes_to=["mid"],
    )

    async def _mid_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join(allows=[make_mid_method])
        if router.claim() == 1:
            yield use_pool_method
        else:
            router.crasher_staged.set()
            await router.crash_release.wait()
            yield crash_method
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
    pool_thread = ThreadTemplate(
        labware_template=pool,
        start=scaffold.pool_pad,
        end=scaffold.pool_pad,
        func=_pool_thread,
    )

    return await finish_closure_system(
        scaffold,
        workflow_name="crash_fail_open_demo",
        system_name="crash_fail_open_system",
        feeder_thread=feeder_thread,
        mid_thread=mid_thread,
        pool_thread=pool_thread,
    )


@pytest.mark.asyncio
async def test_fatal_crash_fails_execution_and_clears_recorded_waiter() -> None:
    recorder = _Recorder()
    router = _MidRouter()
    system, workflow, event_bus = await _build_system(recorder, router)
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
        execution = runtime._executions[eid]

        # Steady pre-crash state: contribution #1 consumed, the pool receiver
        # looped back and is RECORDED awaiting #2, the crasher parked at its gate.
        await wait_until(
            lambda: (
                recorder.use_pool_runs == 1
                and router.crasher_staged.is_set()
                and (slot := pool_slot(runtime, eid)) is not None
                and bool(slot.awaiting_threads)
            ),
            timeout=40.0,
            message="pool receiver never recorded awaiting the second contribution",
        )
        slot = pool_slot(runtime, eid)
        assert slot is not None
        assert len(slot.awaiting_threads) == 1
        assert not slot.is_closed
        pool_status = next(
            t.status for t in runtime.list_threads(eid)
            if t.name.startswith("pool")
        )
        assert pool_status == "AWAITING_CO_THREADS", (
            "a receiver recorded awaiting a join must be VISIBLY waiting "
            f"(operators + stall detector read the status), got {pool_status}"
        )

        router.crash_release.set()

        await wait_until(
            lambda: execution.phase is ExecutionPhase.FAILED,
            timeout=40.0,
            message="ABORT-policy crash never failed the execution",
        )
        await wait_until(
            lambda: not slot.awaiting_threads,
            timeout=40.0,
            message="fatal teardown left a stale awaiting-join waiter recorded",
        )
        await wait_until(
            lambda: execution.task.done(),
            timeout=40.0,
            message="execution task never finished after the fatal teardown",
        )
        assert execution.error is not None and "deliberate crash" in execution.error
    finally:
        router.crash_release.set()
        await runtime.shutdown()
