"""Every operator verb that changes runtime state writes an audit row.

These verbs used to write none: stop, remove, confirm-manual-step, the three
recoverable-timeout decisions, discharge, clear-submission, clear-all and
connect. An operator's reason, when given, rides on the row.
"""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import inspect

import pytest
import pytest_asyncio

from orca.operations.execution import RemoveExecutionOperation, StopExecutionOperation
from orca.operations.execution_models import RemoveExecutionRequest, StopExecutionRequest
from orca.operations.labware import (
    ClearAllLabwareOperation,
    ClearSubmissionLabwareOperation,
    DischargeLabwareOperation,
)
from orca.operations.labware_models import (
    ClearAllLabwareRequest,
    ClearSubmissionLabwareRequest,
    DischargeLabwareRequest,
)
from orca.operations.manual_step import ConfirmManualStepOperation
from orca.operations.manual_step_models import ConfirmManualStepRequest
from orca.runtime.audit_store import AuditEntry
from orca.runtime.danger import DangerLevel
from orca.runtime.facades.incidents import IncidentFacade
from orca.runtime.facades.submissions import SubmissionFacade
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime

from tests.test_system_runtime import _build_simple_system
from tests.audit_capture import AuditCapture
from tests.memory_activity import memory_runtime


@pytest_asyncio.fixture
async def runtime() -> AsyncIterator[SystemRuntime]:
    system, _ = await _build_simple_system()
    rt = await memory_runtime(system)
    await rt.start()
    try:
        yield rt
    finally:
        await rt.shutdown(confirm=True)


async def _finished_run(rt: SystemRuntime) -> tuple[str, str]:
    snap = await rt.submissions.submit_group(
        workflow_name="simple_workflow", mode=WorkflowRunMode.CHECK_RUN, confirm=True,
    )
    await rt.wait(snap.execution_id)
    return snap.execution_id, snap.id


def _the_row(audit: AuditCapture, action_name: str) -> AuditEntry:
    all_rows = audit.rows()
    rows = [r for r in all_rows if r.action_name == action_name]
    assert len(rows) == 1, f"expected one {action_name} row, got {all_rows}"
    return rows[0]


@pytest.mark.asyncio
async def test_discharge_writes_a_row_carrying_the_reason(audit: AuditCapture, runtime: SystemRuntime) -> None:
    snap = await runtime.labware.register("plate_96", location="pad1", confirm=True)

    await DischargeLabwareOperation(runtime).run(
        DischargeLabwareRequest(labware_id=snap.id, reason="picked it up"),
    )

    row = _the_row(audit, "labware.discharge")
    assert row.reason == "picked it up"
    assert row.danger_level == DangerLevel.OPERATOR.name


@pytest.mark.asyncio
async def test_clear_all_writes_a_row(audit: AuditCapture, runtime: SystemRuntime) -> None:
    await ClearAllLabwareOperation(runtime).run(ClearAllLabwareRequest(reason="bench emptied"))

    row = _the_row(audit, "labware.clear_all")
    assert row.reason == "bench emptied"
    assert row.danger_level == DangerLevel.CRITICAL.name


@pytest.mark.asyncio
async def test_clear_submission_writes_a_row(audit: AuditCapture, runtime: SystemRuntime) -> None:
    _, submission_id = await _finished_run(runtime)

    await ClearSubmissionLabwareOperation(runtime).run(
        ClearSubmissionLabwareRequest(submission_id=submission_id, reason="run done"),
    )

    assert _the_row(audit, "labware.clear_submission").reason == "run done"


@pytest.mark.asyncio
async def test_stop_writes_a_row_and_still_reads_confirm_as_the_abort(
    audit: AuditCapture, runtime: SystemRuntime,
) -> None:
    execution_id, _ = await _finished_run(runtime)

    response = await StopExecutionOperation(runtime).run(
        StopExecutionRequest(execution_id=execution_id, confirm=True, reason="wrong plate"),
    )

    assert response.status == "already_terminal"
    row = _the_row(audit, "execution.stop")
    assert row.reason == "wrong plate"
    assert row.call_args["abort"] is True


@pytest.mark.asyncio
async def test_remove_writes_a_row(audit: AuditCapture, runtime: SystemRuntime) -> None:
    execution_id, _ = await _finished_run(runtime)

    await RemoveExecutionOperation(runtime).run(
        RemoveExecutionRequest(execution_id=execution_id, reason="tidy"),
    )

    assert _the_row(audit, "execution.remove").reason == "tidy"


@pytest.mark.asyncio
async def test_confirm_manual_step_writes_a_row(audit: AuditCapture) -> None:
    runtime_access = MagicMock()
    runtime_access.confirm_manual_step = AsyncMock()
    runtime = MagicMock()
    runtime.submissions = SubmissionFacade(runtime_access)

    await ConfirmManualStepOperation(runtime).run(
        ConfirmManualStepRequest(execution_id="e1", step_id="s1", reason="lid off"),
    )

    runtime_access.confirm_manual_step.assert_awaited_once_with("e1", "s1")
    assert _the_row(audit, "execution.confirm_manual_step").reason == "lid off"


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,kwargs", [
    ("extend", {"additional_seconds": 30.0}),
    ("abort", {"operator": "ana"}),
    ("mark_complete", {"operator": "ana"}),
])
async def test_each_recoverable_timeout_decision_writes_a_row(
    audit: AuditCapture, decision: str, kwargs: dict[str, float | str],
) -> None:
    held = MagicMock()
    unknown_here = MagicMock(get=AsyncMock(side_effect=KeyError("inc-1")))
    facade = IncidentFacade(unknown_here, held, MagicMock(), AsyncMock())
    decide = getattr(facade, f"recoverable_timeout_{decision}")

    answer = decide("inc-1", **kwargs, reason="checked the device", confirm=True)
    if inspect.isawaitable(answer):
        await answer

    getattr(held, decision).assert_called_once()
    row = _the_row(audit, f"recoverable_timeout.{decision}")
    assert row.reason == "checked the device"
    assert row.danger_level == DangerLevel.CRITICAL.name


