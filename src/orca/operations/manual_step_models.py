"""Wire models for the manual_step operations.

Apart from `manual_step.py` because the Operation classes there take
`ISystemRuntime`, and the CLI reads these models over HTTP without
ever wanting the engine.
"""

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict
from typing_extensions import Self
from orca.runtime.status_models import PendingManualStepRecord


class ConfirmManualStepRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str
    step_id: str


class ConfirmManualStepResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["confirmed"] = "confirmed"
    execution_id: str
    step_id: str


class PendingManualStepDTO(BaseModel):
    """Mirrors `PendingManualStepRecord` on the wire."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str
    step_id: str
    instruction: str
    emitted_at: datetime

    @classmethod
    def from_record(cls, record: PendingManualStepRecord) -> Self:
        return cls(
            execution_id=record.execution_id,
            step_id=record.step_id,
            instruction=record.instruction,
            emitted_at=record.emitted_at,
        )


class ListPendingManualStepsRequest(BaseModel):
    """`execution_id=None` spans all executions; a value scopes to one."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str | None = None


class ListPendingManualStepsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    pending: tuple[PendingManualStepDTO, ...]
