"""Operator manual-step Operations: list pending + confirm.

`ctx.manual_step(instruction)` parks a thread until an operator confirms
it. These Operations are the operator surface: list the emitted-but-
unconfirmed steps, and confirm one (SAFE: no confirmation gate, fires
immediately). An unknown execution / step_id, an already-confirmed or
timed-out step, or a not-yet-started execution all map to not-found.
"""

from typing import ClassVar
from pydantic import BaseModel
from orca.operations._protocol import OperationError
from orca.runtime.runtime_interface import ISystemRuntime
from orca.operations.manual_step_models import (
    ConfirmManualStepRequest,
    ConfirmManualStepResponse,
    ListPendingManualStepsRequest,
    ListPendingManualStepsResponse,
    PendingManualStepDTO,
)


class ConfirmManualStepOperation:
    Request: ClassVar[type[BaseModel]] = ConfirmManualStepRequest
    Response: ClassVar[type[BaseModel]] = ConfirmManualStepResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: ConfirmManualStepRequest) -> ConfirmManualStepResponse:
        try:
            await self._runtime.confirm_manual_step(req.execution_id, req.step_id)
        except KeyError as exc:
            raise OperationError.not_found(
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
    Request: ClassVar[type[BaseModel]] = ListPendingManualStepsRequest
    Response: ClassVar[type[BaseModel]] = ListPendingManualStepsResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(
        self, req: ListPendingManualStepsRequest,
    ) -> ListPendingManualStepsResponse:
        try:
            records = self._runtime.list_pending_manual_steps(req.execution_id)
        except KeyError as exc:
            raise OperationError.not_found(
                f"execution {req.execution_id!r} not found",
                execution_id=req.execution_id,
            ) from exc
        return ListPendingManualStepsResponse(
            pending=tuple(PendingManualStepDTO.from_record(r) for r in records),
        )
