"""A cancelled wait_for_execution raises; it never returns a mid-flight snapshot.

The old swallow returned a snapshot on cancellation, so asyncio.wait_for
around this method could not time out: callers received partial state that
read as a finished run and asserted on it (a full certification round was
mis-triaged this way).
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from orca.runtime.execution import Execution, ExecutionPhase
from orca.runtime.status_models import ExecutionStatus
from orca.runtime.submission import Submission

from tests.test_helpers import wait_until
from tests.test_thread_mutation import Fixture, _build_system


def _inject_blocked_execution(
    fixture: Fixture, execution_id: str,
) -> tuple[asyncio.Event, asyncio.Task[None]]:
    release = asyncio.Event()
    runtime = fixture.runtime

    async def _blocked() -> None:
        await release.wait()

    task = asyncio.create_task(_blocked())
    runtime._executions[execution_id] = Execution(
        id=execution_id,
        workflow_name=fixture.workflow.name,
        workflow=fixture.workflow,
        system=runtime.system,
        task=task,
        phase=ExecutionPhase.ACCEPTING,
    )
    return release, task


def _submission(execution_id: str) -> Submission:
    submission = MagicMock(spec=Submission)
    submission.execution_id = execution_id
    return submission


class TestWaitForExecutionCancellation:

    @pytest.mark.asyncio
    async def test_cancelling_the_wait_raises(self) -> None:
        fixture = await _build_system()
        release, task = _inject_blocked_execution(fixture, "exec-blocked")
        # Once `started` is set the waiter has run to its first suspension
        # INSIDE wait_for_execution: the cancel hits the engine's wait, not an unstarted task.
        started = asyncio.Event()

        async def _wait() -> ExecutionStatus:
            started.set()
            return await fixture.runtime.wait_for_execution(
                _submission("exec-blocked")
            )

        waiter = asyncio.ensure_future(_wait())
        await wait_until(started.is_set)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        await task

    @pytest.mark.asyncio
    async def test_wait_for_timeout_raises_instead_of_snapshotting(self) -> None:
        fixture = await _build_system()
        release, task = _inject_blocked_execution(fixture, "exec-blocked")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                fixture.runtime.wait_for_execution(_submission("exec-blocked")),
                timeout=0.2,
            )
        release.set()
        await task
