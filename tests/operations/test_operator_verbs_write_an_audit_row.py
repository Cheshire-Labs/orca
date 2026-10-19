"""Every operator verb that changes runtime state writes an audit row.

These verbs used to write none: stop, remove, confirm-manual-step, the three
recoverable-timeout decisions, discharge, clear-submission, clear-all and
connect. An operator's reason, when given, rides on the row.
"""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from orca.operations.device import ConnectDeviceOperation
from orca.operations.device_models import ConnectDeviceRequest
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
from orca.operations.submission import SubmitExecutionOperation
from orca.operations.submission_models import SubmitExecutionRequest
from orca.operations.manual_step_models import ConfirmManualStepRequest
from orca.runtime.danger import AuditEntry, DangerLevel, clear_audit_trail, list_audit_entries
from orca.runtime.facades.incidents import IncidentFacade
from orca.runtime.facades.submissions import SubmissionFacade
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime

from tests.test_system_runtime import _build_simple_system


@pytest.fixture(autouse=True)
def _empty_audit_trail() -> None:
    clear_audit_trail()


@pytest_asyncio.fixture
async def runtime() -> AsyncIterator[SystemRuntime]:
    system, _ = await _build_simple_system()
    rt = SystemRuntime(system, labware_store=InMemoryLabwareStore())
    await rt.start()
    try:
        yield rt
    finally:
        await rt.shutdown(confirm=True)


async def _finished_run(rt: SystemRuntime) -> tuple[str, str]:
    snap = await rt.submissions.submit_group(
        workflow_name="simple_workflow", mode=WorkflowRunMode.PURE_SIM, confirm=True,
    )
    await rt.wait(snap.execution_id)
    return snap.execution_id, snap.id


def _the_row(action_name: str) -> AuditEntry:
    rows = [r for r in list_audit_entries() if r.action_name == action_name]
    assert len(rows) == 1, f"expected one {action_name} row, got {list_audit_entries()}"
    return rows[0]


@pytest.mark.asyncio
async def test_discharge_writes_a_row_carrying_the_reason(runtime: SystemRuntime) -> None:
    snap = await runtime.labware.register("plate_96", location="pad1", confirm=True)

    await DischargeLabwareOperation(runtime).run(
        DischargeLabwareRequest(labware_id=snap.id, reason="picked it up"),
    )

    row = _the_row("labware.discharge")
    assert row.reason == "picked it up"
    assert row.danger_level is DangerLevel.OPERATOR


@pytest.mark.asyncio
async def test_clear_all_writes_a_row(runtime: SystemRuntime) -> None:
    await ClearAllLabwareOperation(runtime).run(ClearAllLabwareRequest(reason="bench emptied"))

    row = _the_row("labware.clear_all")
    assert row.reason == "bench emptied"
    assert row.danger_level is DangerLevel.CRITICAL


@pytest.mark.asyncio
async def test_clear_submission_writes_a_row(runtime: SystemRuntime) -> None:
    _, submission_id = await _finished_run(runtime)

    await ClearSubmissionLabwareOperation(runtime).run(
        ClearSubmissionLabwareRequest(submission_id=submission_id, reason="run done"),
    )

    assert _the_row("labware.clear_submission").reason == "run done"


@pytest.mark.asyncio
async def test_stop_writes_a_row_and_still_reads_confirm_as_the_abort(
    runtime: SystemRuntime,
) -> None:
    execution_id, _ = await _finished_run(runtime)

    response = await StopExecutionOperation(runtime).run(
        StopExecutionRequest(execution_id=execution_id, confirm=True, reason="wrong plate"),
    )

    assert response.status == "already_terminal"
    row = _the_row("execution.stop")
    assert row.reason == "wrong plate"
    assert row.call_args["abort"] is True


@pytest.mark.asyncio
async def test_remove_writes_a_row(runtime: SystemRuntime) -> None:
    execution_id, _ = await _finished_run(runtime)

    await RemoveExecutionOperation(runtime).run(
        RemoveExecutionRequest(execution_id=execution_id, reason="tidy"),
    )

    assert _the_row("execution.remove").reason == "tidy"


@pytest.mark.asyncio
async def test_connect_writes_a_row(runtime: SystemRuntime) -> None:
    await ConnectDeviceOperation(runtime).run(
        ConnectDeviceRequest(device_name="shaker1", reason="bring up"),
    )

    assert _the_row("device.connect").reason == "bring up"


@pytest.mark.asyncio
async def test_confirm_manual_step_writes_a_row() -> None:
    runtime_access = MagicMock()
    runtime_access.confirm_manual_step = AsyncMock()
    runtime = MagicMock()
    runtime.submissions = SubmissionFacade(runtime_access)

    await ConfirmManualStepOperation(runtime).run(
        ConfirmManualStepRequest(execution_id="e1", step_id="s1", reason="lid off"),
    )

    runtime_access.confirm_manual_step.assert_awaited_once_with("e1", "s1")
    assert _the_row("execution.confirm_manual_step").reason == "lid off"


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,kwargs", [
    ("extend", {"additional_seconds": 30.0}),
    ("abort", {"operator": "ana"}),
    ("mark_complete", {"operator": "ana"}),
])
async def test_each_recoverable_timeout_decision_writes_a_row(
    decision: str, kwargs: dict[str, float | str],
) -> None:
    held = MagicMock()
    decide = getattr(IncidentFacade(MagicMock(), held), f"recoverable_timeout_{decision}")

    decide("inc-1", **kwargs, reason="checked the device", confirm=True)

    getattr(held, decision).assert_called_once()
    row = _the_row(f"recoverable_timeout.{decision}")
    assert row.reason == "checked the device"
    assert row.danger_level is DangerLevel.CRITICAL


@pytest.mark.asyncio
@pytest.mark.parametrize("operator_id,action_name", [
    (None, "submission.submit_workflow"),
    ("ana", "submission.submit_group"),
], ids=["single_group", "multi_group"])
async def test_a_submit_writes_a_row_carrying_the_reason(
    runtime: SystemRuntime, operator_id: str | None, action_name: str,
) -> None:
    """With no operator_id, groups or profile the Operation calls submit_workflow;
    otherwise submit_group."""
    response = await SubmitExecutionOperation(runtime).run(
        SubmitExecutionRequest(
            workflow_name="simple_workflow", run_mode="PURE_SIM", reason="first plate",
            operator_id=operator_id,
        ),
    )
    await runtime.wait(response.execution_id)

    row = _the_row(action_name)
    assert row.call_args["workflow_name"] == "simple_workflow"
    assert row.reason == "first plate"
