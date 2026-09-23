"""A thread killed by an unhandled error lands in the terminal FAILED status.

Pre-fix, the crashed thread froze in whatever non-terminal status it had
(EXECUTING_ACTION, AWAITING_CO_THREADS, ...): ``completed`` never fired for
it, ``has_completed()`` said False forever, and every terminal rollup
misclassified it as still-live. FAILED is crash-only and distinct from
ABORTED (operator chose) and STOPPED (operator stop); the execution still
fails via the existing fatal path, with the original error preserved.
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
    wait_for_boot,
)
from tests.test_helpers import wait_until


async def _build_system() -> tuple[ISystem, WorkflowTemplate, EventBus]:
    scaffold = await build_closure_scaffold()
    feeder = scaffold.feeder
    mid = scaffold.mid
    pool = scaffold.pool

    @orca.action(
        device=scaffold.feeder_station_pool,
        inputs=[feeder],
        failure_policy=FailurePolicy.ABORT,
    )
    async def solo_crash(ctx: ActionContext) -> None:
        raise RuntimeError("deliberate crash: FAILED status probe")

    async def _crash_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield solo_crash
    crash_method = MethodTemplate("crash_method", func=_crash_method)

    async def _feeder_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield crash_method
    feeder_thread = ThreadTemplate(
        labware_template=feeder,
        start=scaffold.feeder_pad,
        end=scaffold.waste,
        func=_feeder_thread,
        contributes_to=[],
    )

    # Never spawned: the workflow shape requires them but no action demands them.
    async def _mid_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join(allows=[crash_method])
    mid_thread = ThreadTemplate(
        labware_template=mid,
        start=scaffold.mid_pad,
        end=scaffold.waste,
        func=_mid_thread,
        contributes_to=[],
    )

    async def _pool_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join(allows=[crash_method])
    pool_thread = ThreadTemplate(
        labware_template=pool,
        start=scaffold.pool_pad,
        end=scaffold.pool_pad,
        func=_pool_thread,
    )

    return await finish_closure_system(
        scaffold,
        workflow_name="failed_status_demo",
        system_name="failed_status_system",
        feeder_thread=feeder_thread,
        mid_thread=mid_thread,
        pool_thread=pool_thread,
    )


async def _build_injection_system(
    release_feeder: asyncio.Event,
) -> tuple[ISystem, WorkflowTemplate, EventBus]:
    scaffold = await build_closure_scaffold()
    feeder = scaffold.feeder
    mid = scaffold.mid
    pool = scaffold.pool

    @orca.action(device=scaffold.feeder_station_pool, inputs=[feeder])
    async def safe_step(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=1, speed=500)

    async def _safe_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield safe_step
    safe_method = MethodTemplate("safe_method", func=_safe_method)

    @orca.action(
        device=scaffold.pool_station_pool,
        inputs=[pool],
        failure_policy=FailurePolicy.ABORT,
    )
    async def injected_crash(ctx: ActionContext) -> None:
        raise RuntimeError("deliberate crash: injected-thread rollup probe")

    async def _crash_method(ctx: MethodContext) -> AsyncGenerator[ActionTemplate, None]:
        yield injected_crash
    crash_method = MethodTemplate("crash_method", func=_crash_method)

    async def _feeder_thread(ctx: ThreadContext) -> AsyncGenerator[MethodTemplate, None]:
        yield safe_method
        # Held open so the injection lands while the execution is live.
        await release_feeder.wait()
    feeder_thread = ThreadTemplate(
        labware_template=feeder,
        start=scaffold.feeder_pad,
        end=scaffold.waste,
        func=_feeder_thread,
        contributes_to=[],
    )

    async def _mid_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield orca.join(allows=[safe_method])
    mid_thread = ThreadTemplate(
        labware_template=mid,
        start=scaffold.mid_pad,
        end=scaffold.waste,
        func=_mid_thread,
        contributes_to=[],
    )

    # Never auto-spawned (nothing demands pool); injected by the operator.
    async def _crasher_thread(ctx: ThreadContext) -> AsyncGenerator[IMethodTemplate, None]:
        yield crash_method
    crasher_thread = ThreadTemplate(
        labware_template=pool,
        start=scaffold.pool_pad,
        end=scaffold.pool_pad,
        func=_crasher_thread,
    )

    return await finish_closure_system(
        scaffold,
        workflow_name="injected_crash_demo",
        system_name="injected_crash_system",
        feeder_thread=feeder_thread,
        mid_thread=mid_thread,
        pool_thread=crasher_thread,
    )


@pytest.mark.asyncio
async def test_injected_thread_crash_rolls_up_failed_without_hanging() -> None:
    """The clean-return rollup: a crashed injected thread lands FAILED, sets
    ``completed`` (so wait_all_threads returns instead of hanging forever, the
    pre-fix behavior), and the execution terminates FAILED with an error naming
    the thread and what killed it."""
    release_feeder = asyncio.Event()
    system, workflow, event_bus = await _build_injection_system(release_feeder)
    runtime = SystemRuntime(system, event_bus=event_bus)
    await runtime.start()
    try:
        sub = await runtime.submit(
            workflow,
            groups=[feeder_group("grp-1")],
            batch_mode=BatchMode.STANDALONE,
            mode=WorkflowRunMode.PURE_SIM,
        )
        eid = sub.execution_id
        await wait_for_boot(runtime, eid)

        snapshot = await runtime.spawn_thread_in_execution(eid, "pool")
        await wait_until(
            lambda: any(
                t.id == snapshot.id and t.status == "FAILED"
                for t in runtime.list_threads(eid)
            ),
            timeout=40.0,
            message="injected thread never landed terminal FAILED",
        )

        release_feeder.set()
        execution = runtime._executions[eid]
        await wait_until(
            lambda: execution.task.done(),
            timeout=40.0,
            message="execution hung after the injected-thread crash (pre-fix bug)",
        )
        await wait_until(
            lambda: execution.phase is ExecutionPhase.FAILED,
            timeout=10.0,
            message=f"clean-return rollup must be FAILED, got {execution.phase}",
        )
        assert execution.error is not None
        assert snapshot.name in execution.error, (
            f"the run has to say which thread died; got {execution.error!r}"
        )
        assert "deliberate crash" in execution.error, (
            "the run is the only place that states why it failed, so it has to "
            f"carry the cause; got {execution.error!r}"
        )
    finally:
        release_feeder.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_crashed_thread_lands_terminal_failed() -> None:
    system, workflow, event_bus = await _build_system()
    runtime = SystemRuntime(system, event_bus=event_bus)
    await runtime.start()
    try:
        sub = await runtime.submit(
            workflow,
            groups=[feeder_group("grp-1")],
            batch_mode=BatchMode.STANDALONE,
            mode=WorkflowRunMode.PURE_SIM,
        )
        eid = sub.execution_id
        await wait_for_boot(runtime, eid)

        execution = runtime._executions[eid]
        await wait_until(
            lambda: execution.phase is ExecutionPhase.FAILED,
            timeout=40.0,
            message="execution never reached FAILED after the ABORT-policy crash",
        )
        assert execution.error is not None
        assert "deliberate crash" in execution.error

        wf = execution.executing_workflow
        assert wf is not None
        feeder_exec = next(
            t for t in wf.threads
            if t.thread_instance.thread_template is not None
            and t.thread_instance.thread_template.name == "feeder"
        )
        # Red on main: the crashed thread freezes in a NON-terminal status and
        # never fires ``completed``.
        await wait_until(
            lambda: feeder_exec.status.name == "FAILED",
            timeout=10.0,
            message=(
                "crashed thread must land terminal FAILED, "
                f"got {feeder_exec.status.name}"
            ),
        )
        assert feeder_exec.has_completed()
        assert feeder_exec.completed.is_set()

        # FAILED is crash-only: nothing else in this run may carry it.
        others = [
            t for t in wf.threads if t is not feeder_exec
        ]
        assert all(t.status.name != "FAILED" for t in others)
    finally:
        await runtime.shutdown()
