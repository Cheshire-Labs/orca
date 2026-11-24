"""A run's status is written at each transition, so the store says RUNNING while it runs."""

import asyncio

import pytest

from orca.runtime.execution_record import ExecutionState
from orca.modes.run_modes import WorkflowRunMode
from tests.memory_activity import memory_runtime
from tests.test_system_runtime import _build_simple_system

pytestmark = pytest.mark.asyncio


async def test_a_run_reads_running_in_the_store_while_it_runs() -> None:
    """The stored row says RUNNING from the moment the runtime accepts the run, not only at its end."""
    system, workflow = await _build_simple_system()
    shaker = system.get_device("shaker1")
    in_the_shake = asyncio.Event()
    release = asyncio.Event()
    real_shake = shaker.shake

    async def held_shake(duration: int, speed: int) -> None:
        in_the_shake.set()
        await release.wait()
        await real_shake(duration, speed)

    setattr(shaker, "shake", held_shake)
    runtime = await memory_runtime(system)
    await runtime.start()
    try:
        record = await runtime.submit_workflow(workflow.name, mode=WorkflowRunMode.CHECK_RUN)
        await asyncio.wait_for(in_the_shake.wait(), timeout=20.0)

        stored = await runtime.execution_records.get_detail(record.id)
        assert stored is not None
        assert stored.status == ExecutionState.RUNNING.value

        release.set()
        await runtime.wait(record.id)
    finally:
        release.set()
        await runtime.shutdown(confirm=True)
