"""Incident wire models and routes."""

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator

from orca.operations.wire_models import IncidentResponse
from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag
from orca.operations.wire_models import IncidentAckResponse, RecoverableTimeoutDecisionResponse


class ListIncidentsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    unacknowledged_only: bool = False
    category: str | None = None
    execution_id: str | None = None


class IncidentListResponse(RootModel[list[IncidentResponse]]):
    model_config = ConfigDict(frozen=True)


class GetIncidentRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    incident_id: str


class AcknowledgeIncidentRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    incident_id: str
    reason: str | None = None


class AcknowledgeAllIncidentsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    category: str | None = None
    reason: str | None = None


class ExtendRecoverableTimeoutRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    incident_id: str
    additional_seconds: float = Field(gt=0)
    reason: str | None = None


class _RecoverableTimeoutOperatorRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    incident_id: str
    operator_name: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("operator_name", "reason", mode="after")
    @classmethod
    def _reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-empty and non-whitespace")
        return value


class AbortRecoverableTimeoutRequest(_RecoverableTimeoutOperatorRequest):
    pass


class MarkRecoverableTimeoutCompleteRequest(_RecoverableTimeoutOperatorRequest):
    pass


LIST_INCIDENTS_ROUTE = Route(
    ListIncidentsRequest,
    IncidentListResponse,
    RestBinding(
        method="GET", path="/incidents", tag=RouteTag.INCIDENTS,
    ),
)


GET_INCIDENT_ROUTE = Route(
    GetIncidentRequest,
    IncidentResponse,
    RestBinding(
        method="GET", path="/incidents/{incident_id}", tag=RouteTag.INCIDENTS,
    ),
)


ACKNOWLEDGE_INCIDENT_ROUTE = Route(
    AcknowledgeIncidentRequest,
    IncidentAckResponse,
    RestBinding(
        method="POST", path="/incidents/{incident_id}/ack", tag=RouteTag.INCIDENTS,
    ),
)


ACKNOWLEDGE_ALL_INCIDENTS_ROUTE = Route(
    AcknowledgeAllIncidentsRequest,
    IncidentAckResponse,
    RestBinding(
        method="POST", path="/incidents/ack-all", tag=RouteTag.INCIDENTS,
    ),
)


EXTEND_RECOVERABLE_TIMEOUT_ROUTE = Route(
    ExtendRecoverableTimeoutRequest,
    RecoverableTimeoutDecisionResponse,
    RestBinding(
        method="POST", path="/incidents/{incident_id}/recoverable-timeout/extend",
        tag=RouteTag.INCIDENTS,
        carries_blockers=True,
    ),
)


ABORT_RECOVERABLE_TIMEOUT_ROUTE = Route(
    AbortRecoverableTimeoutRequest,
    RecoverableTimeoutDecisionResponse,
    RestBinding(
        method="POST", path="/incidents/{incident_id}/recoverable-timeout/abort",
        tag=RouteTag.INCIDENTS,
        carries_blockers=True,
    ),
)


MARK_RECOVERABLE_TIMEOUT_COMPLETE_ROUTE = Route(
    MarkRecoverableTimeoutCompleteRequest,
    RecoverableTimeoutDecisionResponse,
    RestBinding(
        method="POST", path="/incidents/{incident_id}/recoverable-timeout/mark-complete",
        tag=RouteTag.INCIDENTS,
        carries_blockers=True,
    ),
)
