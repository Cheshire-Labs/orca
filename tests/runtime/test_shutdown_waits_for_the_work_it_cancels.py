"""Nothing the runtime started is still on the loop once shutdown returns.

`shutdown` closes the incident, record, access-config, move-defaults,
grip-profile and per-device stores. Anything still running when it does hits a
disposed engine, and an aiosqlite call against one does not fail fast: it never
answers. The task then cannot be cancelled either, because cancellation lands
at its next await and it never reaches one.

In CI that was a teardown nobody could end. pytest-asyncio closes its loop with
`_cancel_all_tasks`, which gathers every survivor, and the gather waited on the
stuck task until pytest-timeout killed the run at 120 seconds. The test body
had passed. The log showed the give-away pair: a workflow thread mid-pick, and
`sqlite3.OperationalError: no active connection`.

`shutdown` already cancels AND awaits the deck reseed tasks, the stall watcher
and the location persist task. The two it did not were the execution tasks and
`_background_tasks` -- the orphan-slot drain and the failed-execution teardown,
which are spawned exactly when a run goes wrong and both of which write to the
stores being closed.
"""

import asyncio

import pytest

from orca.runtime.execution import ExecutionPhase
from orca.runtime.registries import NullGatewayRegistry
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime

from tests.runtime.test_deck_layout_required_on_submit import (
    _build_system_with_lh,
)


async def _started_runtime() -> SystemRuntime:
    system = await _build_system_with_lh(deck_layout=None)
    runtime = SystemRuntime(system, gateway_registry=NullGatewayRegistry())
    await runtime.start()
    return runtime


async def _work_that_does_not_end() -> asyncio.Task[None]:
    """A task standing in for one blocked in a store call.

    Started and given one pass of the loop, so it is genuinely running rather
    than merely scheduled.
    """
    task = asyncio.ensure_future(asyncio.sleep(300))
    await asyncio.sleep(0)
    return task


async def test_a_background_task_does_not_outlive_shutdown() -> None:
    """The orphan-slot drain and the failed-execution teardown live here. Both
    write to stores shutdown is about to close."""
    runtime = await _started_runtime()
    task = await _work_that_does_not_end()
    runtime._background_tasks.add(task)

    await runtime.shutdown()

    assert task.done(), (
        "a background task outlived shutdown and will wake up against stores "
        "it already closed"
    )


async def test_every_background_task_is_waited_for_not_just_one() -> None:
    """Control on the loop: cancelling only the first would pass the single
    case and leave the rest running."""
    runtime = await _started_runtime()
    tasks = [await _work_that_does_not_end() for _ in range(3)]
    runtime._background_tasks.update(tasks)

    await runtime.shutdown()

    assert [task.done() for task in tasks] == [True, True, True]


async def test_the_stores_are_closed_after_the_work_stops_not_before() -> None:
    """The ordering is the whole fix. Closing first is what leaves a task
    blocked on a dead connection, and a task blocked there cannot be
    cancelled."""
    runtime = await _started_runtime()
    order: list[str] = []
    task = asyncio.ensure_future(asyncio.sleep(300))
    task.add_done_callback(lambda _: order.append("work stopped"))
    await asyncio.sleep(0)
    runtime._background_tasks.add(task)

    original = runtime._dispose_device_stores

    async def note_then_dispose() -> None:
        order.append("stores closed")
        await original()

    runtime._dispose_device_stores = note_then_dispose  # type: ignore[method-assign]

    await runtime.shutdown()

    assert order == ["work stopped", "stores closed"], order


async def test_an_execution_task_does_not_outlive_shutdown() -> None:
    """Same guarantee for the execution tasks. It holds today only because an
    unrelated await sits between the cancel and the store close, so nothing
    stops a later edit from moving that await."""
    runtime = await _started_runtime()
    record = await runtime.submit_workflow(
        "deck_layout_test_wf", mode=WorkflowRunMode.PURE_SIM,
    )
    execution = runtime._executions[record.id]
    if not execution.task.done():
        execution.task.cancel()
    execution.task = await _work_that_does_not_end()
    execution.phase = ExecutionPhase.ACCEPTING

    await runtime.shutdown()

    assert execution.task.done()


async def test_a_finished_execution_keeps_its_result() -> None:
    """Negative control: a run that completed is not rewritten as aborted on
    the way out."""
    runtime = await _started_runtime()
    record = await runtime.submit_workflow(
        "deck_layout_test_wf", mode=WorkflowRunMode.PURE_SIM,
    )
    execution = runtime._executions[record.id]
    await asyncio.wait_for(asyncio.shield(execution.task), timeout=30)
    finished_phase = execution.phase

    await runtime.shutdown()

    assert not execution.task.cancelled()
    assert execution.phase is finished_phase


@pytest.mark.parametrize("already_done", [True, False])
async def test_shutdown_is_safe_whatever_state_the_work_is_in(
    already_done: bool,
) -> None:
    """Shutdown runs on paths where the background work has already finished
    and on paths where it has not. Neither may raise."""
    runtime = await _started_runtime()
    task = await _work_that_does_not_end()
    if already_done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    runtime._background_tasks.add(task)

    await runtime.shutdown()

    assert task.done()
