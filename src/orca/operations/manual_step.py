from orca.operations.manual_step_models import (
    CONFIRM_MANUAL_STEP_ROUTE,
    LIST_PENDING_MANUAL_STEPS_ROUTE,
)
"""Operator manual-step Operations: list pending + confirm.

`ctx.manual_step(instruction)` parks a thread until an operator confirms
it. These Operations are the operator surface: list the emitted-but-
unconfirmed steps, and confirm one (SAFE: no confirmation gate, fires
immediately). An unknown execution / step_id, an already-confirmed or
timed-out step, or a not-yet-started execution all map to not-found.
"""

from orca.operations.binding import RestBinding
from typing import ClassVar
from pydantic import BaseModel
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.runtime.runtime_interface import ISystemRuntime
from orca.operations.manual_step_models import (
    ConfirmManualStepRequest,
    ConfirmManualStepResponse,
    ListPendingManualStepsRequest,
    ListPendingManualStepsResponse,
    PendingManualStepDTO,
)


class ConfirmManualStepOperation:
    Request: ClassVar[type[BaseModel]] = CONFIRM_MANUAL_STEP_ROUTE.request
    Response: ClassVar[type[BaseModel]] = CONFIRM_MANUAL_STEP_ROUTE.response
    binding: ClassVar[RestBinding] = CONFIRM_MANUAL_STEP_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: ConfirmManualStepRequest) -> ConfirmManualStepResponse:
        try:
            await self._runtime.submissions.confirm_manual_step(
                req.execution_id, req.step_id, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.of(
                ErrorCode.MANUAL_STEP_NOT_FOUND,
                f"manual step {req.step_id!r} not found in execution "
                f"{req.execution_id!r}; run 'manual-step list' to see "
                "pending steps",
                execution_id=req.execution_id,
                step_id=req.step_id,
            ) from exc
        return ConfirmManualStepResponse(
            execution_id=req.execution_id, step_id=req.step_id,
        )


class ListPendingManualStepsOperation:
    Request: ClassVar[type[BaseModel]] = LIST_PENDING_MANUAL_STEPS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_PENDING_MANUAL_STEPS_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_PENDING_MANUAL_STEPS_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(
        self, req: ListPendingManualStepsRequest,
    ) -> ListPendingManualStepsResponse:
        try:
            records = self._runtime.list_pending_manual_steps(req.execution_id)
        except KeyError as exc:
            raise OperationError.of(
                ErrorCode.EXECUTION_NOT_FOUND,
                f"execution {req.execution_id!r} not found",
                execution_id=req.execution_id,
            ) from exc
        return ListPendingManualStepsResponse(
            pending=tuple(PendingManualStepDTO.from_record(r) for r in records),
        )


MANUAL_STEP_OPERATIONS = (
    ConfirmManualStepOperation,
    ListPendingManualStepsOperation,
)
