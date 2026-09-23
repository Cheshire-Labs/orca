"""Finishing does not move an execution to the store.

`GetExecutionDetailOperation` asks the runtime first and only reads the persisted
record when the runtime no longer holds the execution. That matters to every
operator surface: the store keeps `pause_reason` and `last_error` per thread and
nothing else, so a surface that assumed a failed run answers from the store would
tell an operator there was no `pause_message` and no method when both are there.
"""

from unittest.mock import MagicMock

import pytest

from orca.operations.execution import GetExecutionDetailOperation
from orca.operations.execution_models import GetExecutionDetailRequest
from orca.runtime.status_models import ExecutionDetail, MethodSnapshot, ThreadSnapshot


def _live_detail() -> ExecutionDetail:
    thread = ThreadSnapshot(
        id="t1",
        name="plate_1-abcd",
        status="PAUSED",
        current_location="shaker_1",
        current_method=MethodSnapshot(
            id="m1",
            name="run_assay_step",
            status="IN_PROGRESS",
            current_action=None,
            completed_action_count=0,
        ),
        completed_method_count=0,
        last_error="RuntimeError: shaker_1 is not connected",
        pause_reason="error",
        completed_methods=(),
        paused_device_command="shake",
        pause_message="device call failed: shake",
    )
    return ExecutionDetail(
        id="exec-1", workflow_name="smc_assay", status="failed", error=None,
        threads=[thread], total_thread_count=1, completed_thread_count=0,
        active_thread_count=0,
    )


def _stored_detail() -> ExecutionDetail:
    """What the persisted record can rebuild: the status fields, and no more."""
    thread = ThreadSnapshot(
        id="t1",
        name="plate_1-abcd",
        status="PAUSED",
        current_location="",
        current_method=None,
        completed_method_count=0,
        last_error="RuntimeError: shaker_1 is not connected",
        pause_reason="error",
        completed_methods=(),
    )
    return ExecutionDetail(
        id="exec-1", workflow_name="smc_assay", status="failed", error=None,
        threads=[thread], total_thread_count=1, completed_thread_count=0,
        active_thread_count=0,
    )


def _runtime_holding() -> MagicMock:
    runtime = MagicMock()
    runtime.get_execution_detail.return_value = _live_detail()
    return runtime


def _runtime_that_let_go() -> MagicMock:
    runtime = MagicMock()
    runtime.get_execution_detail.side_effect = KeyError("exec-1")
    return runtime


@pytest.mark.asyncio
async def test_a_finished_execution_the_runtime_still_holds_answers_live() -> None:
    store_reads: list[str] = []

    async def lookup(execution_id: str) -> ExecutionDetail | None:
        store_reads.append(execution_id)
        return _stored_detail()

    op = GetExecutionDetailOperation(
        runtime=_runtime_holding(), terminal_detail_lookup=lookup,
    )
    resp = await op.run(GetExecutionDetailRequest(execution_id="exec-1"))

    assert store_reads == [], "the store was read for an execution the runtime still holds"
    thread = resp.threads[0]
    assert thread.pause_message == "device call failed: shake"
    assert thread.paused_device_command == "shake"
    assert thread.current_method is not None


@pytest.mark.asyncio
async def test_only_an_execution_the_runtime_dropped_is_read_from_the_store() -> None:
    async def lookup(execution_id: str) -> ExecutionDetail | None:
        return _stored_detail()

    op = GetExecutionDetailOperation(
        runtime=_runtime_that_let_go(), terminal_detail_lookup=lookup,
    )
    resp = await op.run(GetExecutionDetailRequest(execution_id="exec-1"))

    thread = resp.threads[0]
    assert thread.pause_reason == "error"
    assert thread.last_error == "RuntimeError: shaker_1 is not connected"
    assert thread.pause_message is None
    assert thread.paused_device_command is None
    assert thread.current_method is None
