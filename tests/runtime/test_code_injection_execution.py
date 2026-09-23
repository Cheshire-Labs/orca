"""Execution coverage for the compile-from-source injection path.

`test_code_injection.py` proves `compile_method_code` / `compile_action_code`
PRODUCE a template from wire source; `test_compile_workflow_code.py` proves
`compile_workflow_code` produces a `WorkflowTemplate`. None of them RUN the
result -- a compiler that returned a structurally-valid but non-runnable
template would pass every one of those tests.

These tests close that gap. They drive a compiled-from-source method, action,
and workflow through a live `SystemRuntime` and assert the wrapped generator
actually executed (the device call fired with the injected argument), not just
that a template came back.

Injected method/action source references devices the way production wire
callers do -- via `deployment_package` imports (the only non-orca prefix on
the code-injection allow-list) -- so a fake `deployment_package` module
exposes the live pool/plate objects.
"""

import asyncio
import sys
import textwrap
import types
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from orca.events.execution_context import MethodExecutionContext
from orca.operations.thread import InsertActionOperation, InsertMethodOperation
from orca.operations.thread_models import InsertActionRequest, InsertMethodRequest
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.sinks import CollectorSink
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import ExecutionState, SystemRuntime
from orca.sdk.build import Topology, build_system, compile_workflow_code
from orca.workflow_models.method_template import _PENDING_METHOD_TEMPLATES
from orca.workflow_models.status_enums import RecoveryDecision
from orca.workflow_models.thread_template import _PENDING_THREAD_TEMPLATES
from tests.mutation_helpers import pause_and_wait, wait_for_paused, wait_for_threads
from tests.test_error_recovery import _build_two_action_failing_system
from tests.test_helpers import create_test_device, create_test_transporter
from tests.test_thread_mutation import _build_system


@pytest.fixture(autouse=True)
def _isolate_pending_templates() -> Iterator[None]:
    """Decorator exec appends to the build-time pending queues; snapshot and
    restore so these tests neither inherit nor leak pending templates."""
    saved_methods = list(_PENDING_METHOD_TEMPLATES)
    saved_threads = list(_PENDING_THREAD_TEMPLATES)
    _PENDING_METHOD_TEMPLATES.clear()
    _PENDING_THREAD_TEMPLATES.clear()
    yield
    _PENDING_METHOD_TEMPLATES[:] = saved_methods
    _PENDING_THREAD_TEMPLATES[:] = saved_threads


def _as_runtime(runtime: SystemRuntime) -> ISystemRuntime:
    """SystemRuntime satisfies ISystemRuntime at runtime -- the daemon router
    passes the concrete runtime into these same operations. Pyright's
    structural check rejects it (a pre-existing drift, ~37 production sites),
    so the one suppression lives here rather than at every call site."""
    return runtime  # type: ignore[return-value]


@contextmanager
def _deployment_package(**objects: object) -> Iterator[None]:
    """Install a fake `deployment_package` module exposing `objects` by name.

    Mirrors how wire source addresses live topology devices without a real
    worktree on disk; the code-injection allow-list permits the prefix.
    """
    saved = sys.modules.get("deployment_package")
    module = types.ModuleType("deployment_package")
    for name, value in objects.items():
        setattr(module, name, value)
    sys.modules["deployment_package"] = module
    try:
        yield
    finally:
        if saved is not None:
            sys.modules["deployment_package"] = saved
        else:
            del sys.modules["deployment_package"]


async def test_compiled_method_inserts_and_executes() -> None:
    """`InsertMethodOperation` with `method_code` -> the compiled method runs.

    Pause a thread, inject a method from source via the production operation,
    resume, and assert the injected method completed AND its action drove the
    device with the injected speed.
    """
    f = await _build_system()
    collector = CollectorSink()
    f.runtime.register_sink(collector)

    speed_log: list[int] = []
    original_shake = f.device.shake

    async def tracking_shake(duration: int, speed: int) -> None:
        speed_log.append(speed)
        await original_shake(duration, speed)

    f.device.shake = tracking_shake  # type: ignore[method-assign]

    await f.runtime.start()
    record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
    threads = await wait_for_threads(f.runtime, record.id)
    await pause_and_wait(f.runtime, record.id, threads[0].id)

    method_code = textwrap.dedent("""
        import orca.orca as orca
        from deployment_package import injected_pool, injected_plate

        @orca.action(device=injected_pool, inputs=[injected_plate])
        async def injected_shake(ctx):
            await ctx.device().shake(duration=1, speed=4242)

        @orca.method
        async def injected_method(ctx):
            yield injected_shake
    """)

    op = InsertMethodOperation(runtime=_as_runtime(f.runtime))
    with _deployment_package(injected_pool=f.pool, injected_plate=f.plate):
        await op.run(InsertMethodRequest(
            execution_id=record.id, thread_id=threads[0].id,
            method_code=method_code, where="tail",
            reason="cover the compile-from-source execution path",
        ))

    f.runtime.resume_thread(record.id, threads[0].id)
    status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

    assert status.status == ExecutionState.COMPLETED, f"got {status.status}: {status.error}"
    completed = [
        e.context.method_name for e in collector.events
        if e.entity_type == "METHOD" and e.status == "COMPLETED"
        and isinstance(e.context, MethodExecutionContext)
    ]
    assert "injected_method" in completed, f"injected method never executed: {completed}"
    assert 4242 in speed_log, f"injected action's device call never fired: {speed_log}"

    await f.runtime.shutdown()


async def test_compiled_action_inserts_and_executes() -> None:
    """`InsertActionOperation` with `action_code` -> the compiled action runs.

    Error-pause on a failing action, inject an action from source via the
    production operation, retry, and assert the injected action drove the
    device with the injected argument. This is the only path that exercises
    `compile_action_code` against a real `@orca.action` decorator end to end.
    """
    f = await _build_two_action_failing_system()
    collector = CollectorSink()
    f.runtime.register_sink(collector)

    seal_log: list[int] = []
    original_seal = f.device.seal

    async def tracking_seal(temperature: int, duration: float) -> None:
        seal_log.append(temperature)
        await original_seal(temperature, duration)

    f.device.seal = tracking_seal  # type: ignore[method-assign]

    await f.runtime.start()
    record = await f.runtime.submit_workflow(f.workflow.name, mode=WorkflowRunMode.PURE_SIM)
    thread_id = await wait_for_paused(f.runtime, record.id)

    pool = f.runtime.system.get_resource_pool("shaker1")
    plate = f.runtime.system.get_labware_thread_template(
        f.workflow.name, "plate_96",
    ).labware_template

    action_code = textwrap.dedent("""
        import orca.orca as orca
        from deployment_package import injected_pool, injected_plate

        @orca.action(device=injected_pool, inputs=[injected_plate])
        async def injected_seal(ctx):
            await ctx.device().seal(temperature=137, duration=1)
    """)

    op = InsertActionOperation(runtime=_as_runtime(f.runtime))
    with _deployment_package(injected_pool=pool, injected_plate=plate):
        await op.run(InsertActionRequest(
            execution_id=record.id, thread_id=thread_id,
            action_code=action_code, where="tail",
            reason="cover the compile-from-source execution path",
        ))

    f.device.should_fail = False
    f.runtime.recover_thread(record.id, thread_id, RecoveryDecision.RETRY)
    status = await asyncio.wait_for(f.runtime.wait(record.id), timeout=10.0)

    assert status.status == ExecutionState.COMPLETED, f"got {status.status}: {status.error}"
    assert 137 in seal_log, f"injected action's device call never fired: {seal_log}"

    await f.runtime.shutdown()


async def test_compiled_workflow_runs_to_completion() -> None:
    """`compile_workflow_code(source, topology)` -> submit -> runs to COMPLETED.

    The from-source workflow path, end to end against a real topology: compile
    the source, build a runtime, submit, and assert the workflow completed AND
    its action drove the device with the injected speed.
    """
    shaker = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    topology = Topology(
        locations={"shaker1": shaker, "pad1": PlatePad("pad1")},
        transporters=[transporter],
        pools=[ResourcePool("shaker1", [shaker])],
    )

    speed_log: list[int] = []
    original_shake = shaker.shake

    async def tracking_shake(duration: int, speed: int) -> None:
        speed_log.append(speed)
        await original_shake(duration, speed)

    shaker.shake = tracking_shake  # type: ignore[method-assign]

    source = textwrap.dedent("""
        import orca.orca as orca
        from tests.test_helpers import create_test_plate_template

        def build_workflow(topology):
            plate = create_test_plate_template("plate_96")
            shaker_pool = topology.pool("shaker1")

            @orca.action(device=shaker_pool, inputs=[plate])
            async def shake_action(ctx):
                await ctx.device().shake(duration=1, speed=7777)

            @orca.method
            async def shake_method(ctx):
                yield shake_action

            @orca.thread(labware=plate, start="pad1", end="pad1")
            async def plate_journey(ctx):
                yield shake_method

            @orca.workflow(name="src_workflow")
            def src_workflow(wf):
                wf.start(plate_journey)

            return src_workflow
    """)

    template = compile_workflow_code(source, topology)
    build = await build_system("src_test", topology, InMemoryRuntimeStoreFactory(), workflow=template)
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)

    await runtime.start()
    try:
        record = await runtime.submit_workflow(template.name, mode=WorkflowRunMode.PURE_SIM)
        status = await asyncio.wait_for(runtime.wait(record.id), timeout=10.0)
        assert status.status == ExecutionState.COMPLETED, f"got {status.status}: {status.error}"
        assert 7777 in speed_log, f"compiled workflow's device call never fired: {speed_log}"
    finally:
        await runtime.shutdown()
