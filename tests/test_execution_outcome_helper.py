"""``execution_outcome`` must never let a timed-out wait masquerade as done.

A timeout fails the test with a per-thread state dump, and a wait that
resolves in a non-terminal phase fails loudly too, so no caller ever
asserts on a mid-flight snapshot.
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from _pytest.outcomes import Failed

from orca.runtime.execution import ExecutionPhase
from orca.runtime.status_models import ExecutionStatus
from orca.runtime.submission import Submission
from tests.test_helpers import execution_outcome


def _runtime_returning(phase: ExecutionPhase) -> MagicMock:
    runtime = MagicMock()

    async def _wait(submission: Submission) -> ExecutionStatus:
        return ExecutionStatus(id="e1", workflow_name="wf", status=phase)

    runtime.wait_for_execution = _wait
    runtime.list_threads = MagicMock(return_value=[])
    return runtime


def _submission() -> MagicMock:
    submission = MagicMock(spec=Submission)
    submission.execution_id = "e1"
    return submission


class TestExecutionOutcome:

    @pytest.mark.asyncio
    async def test_non_terminal_snapshot_fails_loudly(self) -> None:
        runtime = _runtime_returning(ExecutionPhase.ACCEPTING)
        with pytest.raises(Failed, match="non-terminal phase"):
            await execution_outcome(runtime, _submission(), timeout=5.0)

    @pytest.mark.asyncio
    # The helper's own 0.2s budget is what is under test, so if it ever stops
    # firing nothing else here bounds the wait on a task that never completes.
    @pytest.mark.timeout(10)
    async def test_timeout_fails_loudly_with_thread_dump(self) -> None:
        runtime = MagicMock()

        async def _hang(submission: Submission) -> ExecutionStatus:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        runtime.wait_for_execution = _hang
        runtime.list_threads = MagicMock(return_value=[])
        with pytest.raises(Failed, match="not terminal after the .* budget"):
            await execution_outcome(runtime, _submission(), timeout=0.2)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("phase", [
        ExecutionPhase.COMPLETED, ExecutionPhase.FAILED, ExecutionPhase.ABORTED,
    ])
    async def test_terminal_phase_returns_the_status(
        self, phase: ExecutionPhase,
    ) -> None:
        runtime = _runtime_returning(phase)
        status = await execution_outcome(runtime, _submission(), timeout=5.0)
        assert status.status is phase
