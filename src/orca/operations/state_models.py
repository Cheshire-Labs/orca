"""Wire models for the state operations.

Apart from `state.py` because the Operation classes there take
`ISystemRuntime`, and the CLI reads these models over HTTP without
ever wanting the engine.
"""

from pydantic import BaseModel, ConfigDict
from typing_extensions import Self
from orca.state.provenance import Provenance
from orca.state.unsettled import UnsettledSubject


class UnsettledSubjectDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    subject: str
    subject_id: str | None
    """The id the verb takes when that is not the subject: a labware id for a
    labware row, null for a head, whose verbs take the device name."""
    subject_kind: str
    provenance: Provenance
    """Why it is unsettled. `unknown` is nobody ever said; `stale` is it was
    known, then a stretch passed with nobody watching."""
    detail: str
    settle_with: str | None
    """The verb that settles it. Null when no verb does: an unfinished action
    is holding operations, and only ending that action helps."""

    @classmethod
    def from_subject(cls, subject: UnsettledSubject) -> Self:
        return cls(
            subject=subject.subject,
            subject_id=subject.subject_id,
            subject_kind=subject.subject_kind,
            provenance=subject.provenance,
            detail=subject.detail,
            settle_with=subject.settle_with,
        )


class UnsettledStateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class UnsettledStateResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    unsettled: list[UnsettledSubjectDTO]
