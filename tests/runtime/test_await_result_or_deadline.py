"""The deadline primitive behind run_with_timeout's device-call race.

Pins the contract that made it worth extracting from ``wait_for(shield(...))``:
it never cancels the awaited task on timeout, a task that finishes wins over a
simultaneous deadline, an already-finished task returns at once, and the task's
own exception propagates unchanged.
"""

import asyncio

import pytest

from orca.runtime.recoverable_timeout import _await_result_or_deadline

# Fail fast if the primitive ever stops timing out, rather than blocking forever
# on the never-completing task these tests await against.
pytestmark = pytest.mark.timeout(5)


async def test_returns_result_when_task_finishes_within_timeout() -> None:
    async def quick() -> str:
        return "ok"

    task = asyncio.ensure_future(quick())
    assert await _await_result_or_deadline(task, timeout=5.0) == "ok"


async def test_already_finished_task_returns_immediately() -> None:
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    fut.set_result("done")
    assert await _await_result_or_deadline(fut, timeout=0.01) == "done"


async def test_timeout_raises_without_cancelling_the_task() -> None:
    task = asyncio.ensure_future(asyncio.Event().wait())
    with pytest.raises(asyncio.TimeoutError):
        await _await_result_or_deadline(task, timeout=0.02)
    # The whole point: the in-flight call survives the deadline so an operator
    # extension keeps awaiting the same task rather than a cancelled stub.
    assert not task.cancelled()
    assert not task.done()
    task.cancel()


async def test_task_exception_propagates_not_timeout() -> None:
    async def boom() -> str:
        raise ValueError("kaboom")

    task = asyncio.ensure_future(boom())
    with pytest.raises(ValueError, match="kaboom"):
        await _await_result_or_deadline(task, timeout=5.0)
