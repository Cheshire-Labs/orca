"""Typed-envelope wire-shape tests for the operator clear Operations.

``ClearSubmissionLabwareOperation``, ``DischargeLabwareOperation``, and
``ClearAllLabwareOperation`` refuse a non-force clear when an active
execution holds a reference. The refusal surfaces as
``OperationError.typed(...)`` with ``wire_code="active_execution_refused"``
and ``status_code=409`` so envelope-aware callers (CLI, a hosted deployment's wire
layer) see the canon-pinned wire shape.
"""

from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

from orca.operations._protocol import OperationError, OperationErrorCode
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
from orca.runtime.runtime_interface import (
    ActiveExecutionRefusedError,
    ClearSubmissionResult,
    LabwareNotFoundError,
)


def _runtime_with_refusal(
    method_name: str,
    refusal: ActiveExecutionRefusedError,
) -> MagicMock:
    runtime = MagicMock()
    runtime.labware = MagicMock()
    setattr(runtime.labware, method_name, AsyncMock(side_effect=refusal))
    return runtime


@pytest.mark.asyncio
async def test_clear_submission_answers_with_both_lists() -> None:
    """What was cleared AND what was left on the deck. An operator told only
    the first cannot tell whether the deck residents survived."""
    runtime = MagicMock()
    runtime.labware = MagicMock()
    runtime.labware.clear_submission_labware = AsyncMock(
        return_value=ClearSubmissionResult(
            cleared=["plate-a"], preserved_reuse_bound=["trough"],
        ),
    )
    op = ClearSubmissionLabwareOperation(runtime=runtime)

    response = await op.run(ClearSubmissionLabwareRequest(submission_id="sub-42"))

    assert response.model_dump() == {
        "cleared": ["plate-a"], "preserved_reuse_bound": ["trough"],
    }


@pytest.mark.asyncio
async def test_clear_submission_active_execution_refused_surfaces_typed_409() -> None:
    refusal = ActiveExecutionRefusedError(
        scope="submission",
        submission_id="sub-42",
    )
    op = ClearSubmissionLabwareOperation(
        runtime=_runtime_with_refusal("clear_submission_labware", refusal),
    )

    with pytest.raises(OperationError) as exc_info:
        await op.run(ClearSubmissionLabwareRequest(submission_id="sub-42"))

    raised = exc_info.value
    assert raised.code is OperationErrorCode.CONFLICT
    assert raised.wire_code == "active_execution_refused"
    assert raised.status_code == 409
    assert raised.extras == {"scope": "submission", "submission_id": "sub-42"}


@pytest.mark.asyncio
async def test_discharge_labware_active_execution_refused_surfaces_typed_409() -> None:
    refusal = ActiveExecutionRefusedError(
        scope="labware",
        labware_id="lab-7",
    )
    op = DischargeLabwareOperation(
        runtime=_runtime_with_refusal("discharge_labware", refusal),
    )

    with pytest.raises(OperationError) as exc_info:
        await op.run(DischargeLabwareRequest(labware_id="lab-7"))

    raised = exc_info.value
    assert raised.code is OperationErrorCode.CONFLICT
    assert raised.wire_code == "active_execution_refused"
    assert raised.status_code == 409
    assert raised.extras == {"scope": "labware", "labware_id": "lab-7"}


@pytest.mark.asyncio
async def test_clear_all_active_execution_refused_surfaces_typed_409() -> None:
    scope: Literal["all"] = "all"
    refusal = ActiveExecutionRefusedError(scope=scope)
    op = ClearAllLabwareOperation(
        runtime=_runtime_with_refusal("clear_all_labware", refusal),
    )

    with pytest.raises(OperationError) as exc_info:
        await op.run(ClearAllLabwareRequest())

    raised = exc_info.value
    assert raised.code is OperationErrorCode.CONFLICT
    assert raised.wire_code == "active_execution_refused"
    assert raised.status_code == 409
    assert raised.extras == {"scope": "all"}


@pytest.mark.asyncio
async def test_discharging_a_labware_nobody_knows_reads_as_a_sentence() -> None:
    """`LabwareNotFoundError` subclasses `KeyError`, so `str()` on it is a repr
    and the operator's message arrives wrapped in a second pair of quotes."""
    runtime = MagicMock()
    runtime.labware = MagicMock()
    runtime.labware.discharge_labware = AsyncMock(
        side_effect=LabwareNotFoundError("lab-404"),
    )
    op = DischargeLabwareOperation(runtime=runtime)

    with pytest.raises(OperationError) as exc_info:
        await op.run(DischargeLabwareRequest(labware_id="lab-404"))

    raised = exc_info.value
    assert raised.code is OperationErrorCode.NOT_FOUND
    assert raised.message == "No labware with id 'lab-404'", raised.message
    assert raised.extras == {"labware_id": "lab-404"}
