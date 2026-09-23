"""Fast full-runtime pin of the FEEDER-dead close branch.

The fast suite's other closure e2es (the premature-close probe and the
crash-fail-open pin) both close their pool via WORKER quiescence, because
their pool sits in no ``contributes_to`` chain. This topology declares the
chain ``feeder -> mid -> pool``, so the pool slot is FEEDER-governed: the
close fires when its transitive feeders have all terminated, deterministically
via the feeder sweep (while any feeder or mid executes, it counts as a worker
for every slot, so the worker sweep can never close the pool first). The
caplog assertion pins the branch, not just the outcome.

Pinned end to end on a real SystemRuntime: both contributions served, close
via FEEDER-dead, the receiver exits its has_more_work loop, finalizes on the
FULL fill, and the execution quiesces. Coverage promotion from the closure
generality audit: the branch previously had full-runtime coverage only in
slow-marked SMC runs.
"""

import logging
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
)
from tests.test_helpers import wait_until


class _Recorder:
    def __init__(self) -> None:
        self.use_pool_runs = 0
        self.finalize_snapshots: list[int] = []


async def _build_system(recorder: _Recorder) -> tuple[ISystem, WorkflowTemplate, EventBus]:
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
        yield use_pool_method
    mid_thread = ThreadTemplate(
        labware_template=mid,
        start=scaffold.mid_pad,
        end=scaffold.waste,
        func=_mid_thread,
        contributes_to=["pool"],
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
        workflow_name="feeder_close_demo",
        system_name="feeder_close_system",
        feeder_thread=feeder_thread,
        mid_thread=mid_thread,
        pool_thread=pool_thread,
    )


@pytest.mark.asyncio
async def test_feeder_dead_close_finalizes_on_full_fill(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="orca")
    recorder = _Recorder()
    system, workflow, event_bus = await _build_system(recorder)
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

        statuses: dict[str, str] = {}

        def _all_terminal() -> bool:
            nonlocal statuses
            statuses = {t.name: t.status for t in runtime.list_threads(eid)}
            return bool(statuses) and all(s in TERMINAL_STATUSES for s in statuses.values())

        await wait_until(
            _all_terminal,
            timeout=60.0,
            message=f"execution never quiesced; statuses={statuses}",
        )
    finally:
        await runtime.shutdown()

    assert recorder.use_pool_runs == 2, (
        f"both contributions must be served, got {recorder.use_pool_runs}"
    )
    assert recorder.finalize_snapshots == [2], (
        "the pool must finalize exactly once, on the FULL fill, got "
        f"{recorder.finalize_snapshots}"
    )
    assert all(s == "COMPLETED" for s in statuses.values()), (
        f"every thread must COMPLETE cleanly; statuses={statuses}"
    )

    pool_closes = [
        r.getMessage() for r in caplog.records
        if "Slot close" in r.getMessage() and "pool" in r.getMessage()
    ]
    assert pool_closes, "the pool slot was never closed by the evaluator"
    assert all("[FEEDER-dead]" in m for m in pool_closes), (
        "a contributes_to-governed pool must close via the FEEDER branch, "
        f"got: {pool_closes}"
    )
