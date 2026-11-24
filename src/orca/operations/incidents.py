"""Incident Operations: read, acknowledge, and decide a recoverable timeout."""

from typing import ClassVar

from pydantic import BaseModel

from orca.operations.wire_models import (
    IncidentAckResponse,
    IncidentResponse,
    RecoverableTimeoutDecisionResponse,
)
from orca.operations.binding import RestBinding
from orca.operations.error_codes import ErrorCode
from orca.operations.labware import slot_occupied
from orca.operations.protocol import OperationError
from orca.resource_models.device_error import SlotOccupiedError
from orca.runtime.incident_store import IncidentCategory
from orca.runtime.runtime_interface import ISystemRuntime
from orca.operations.incident_models import (
    ABORT_RECOVERABLE_TIMEOUT_ROUTE,
    ACKNOWLEDGE_ALL_INCIDENTS_ROUTE,
    ACKNOWLEDGE_INCIDENT_ROUTE,
    EXTEND_RECOVERABLE_TIMEOUT_ROUTE,
    GET_INCIDENT_ROUTE,
    LIST_INCIDENTS_ROUTE,
    MARK_RECOVERABLE_TIMEOUT_COMPLETE_ROUTE,
)
from orca.operations.incident_models import (
    AbortRecoverableTimeoutRequest,
    AcknowledgeAllIncidentsRequest,
    AcknowledgeIncidentRequest,
    ExtendRecoverableTimeoutRequest,
    GetIncidentRequest,
    IncidentListResponse,
    ListIncidentsRequest,
    MarkRecoverableTimeoutCompleteRequest,
)


def _category_of(name: str | None) -> IncidentCategory | None:
    if name is None:
        return None
    try:
        return IncidentCategory[name.upper()]
    except KeyError as exc:
        raise OperationError(
            ErrorCode.VALIDATION_ERROR,
            f"unknown incident category {name!r}; valid: {[c.name for c in IncidentCategory]}",
        ) from exc


def _no_such_incident(incident_id: str) -> OperationError:
    return OperationError.of(ErrorCode.INCIDENT_NOT_FOUND, f"incident {incident_id!r} not found")


def _no_held_command(incident_id: str) -> OperationError:
    return OperationError.of(
        ErrorCode.HELD_COMMAND_NOT_FOUND,
        f"no held command found for recoverable-timeout incident {incident_id!r}",
    )


class ListIncidentsOperation:
    Request: ClassVar[type[BaseModel]] = LIST_INCIDENTS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_INCIDENTS_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_INCIDENTS_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: ListIncidentsRequest) -> IncidentListResponse:
        incidents = await self._runtime.incidents.list(
            unacknowledged_only=req.unacknowledged_only,
            category=_category_of(req.category),
            execution_id=req.execution_id,
        )
        return IncidentListResponse([IncidentResponse.from_incident(i) for i in incidents])


class GetIncidentOperation:
    Request: ClassVar[type[BaseModel]] = GET_INCIDENT_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_INCIDENT_ROUTE.response
    binding: ClassVar[RestBinding] = GET_INCIDENT_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: GetIncidentRequest) -> IncidentResponse:
        try:
            incident = await self._runtime.incidents.get(req.incident_id)
        except KeyError as exc:
            raise _no_such_incident(req.incident_id) from exc
        return IncidentResponse.from_incident(incident)


class AcknowledgeIncidentOperation:
    Request: ClassVar[type[BaseModel]] = ACKNOWLEDGE_INCIDENT_ROUTE.request
    Response: ClassVar[type[BaseModel]] = ACKNOWLEDGE_INCIDENT_ROUTE.response
    binding: ClassVar[RestBinding] = ACKNOWLEDGE_INCIDENT_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: AcknowledgeIncidentRequest) -> IncidentAckResponse:
        try:
            await self._runtime.incidents.acknowledge(
                req.incident_id, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise _no_such_incident(req.incident_id) from exc
        return IncidentAckResponse(acknowledged_count=1)


class AcknowledgeAllIncidentsOperation:
    Request: ClassVar[type[BaseModel]] = ACKNOWLEDGE_ALL_INCIDENTS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = ACKNOWLEDGE_ALL_INCIDENTS_ROUTE.response
    binding: ClassVar[RestBinding] = ACKNOWLEDGE_ALL_INCIDENTS_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: AcknowledgeAllIncidentsRequest) -> IncidentAckResponse:
        count = await self._runtime.incidents.acknowledge_all(
            category=_category_of(req.category), reason=req.reason, confirm=True,
        )
        return IncidentAckResponse(acknowledged_count=count)


class ExtendRecoverableTimeoutOperation:
    Request: ClassVar[type[BaseModel]] = EXTEND_RECOVERABLE_TIMEOUT_ROUTE.request
    Response: ClassVar[type[BaseModel]] = EXTEND_RECOVERABLE_TIMEOUT_ROUTE.response
    binding: ClassVar[RestBinding] = EXTEND_RECOVERABLE_TIMEOUT_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(
        self, req: ExtendRecoverableTimeoutRequest,
    ) -> RecoverableTimeoutDecisionResponse:
        try:
            self._runtime.incidents.recoverable_timeout_extend(
                req.incident_id, req.additional_seconds, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise _no_held_command(req.incident_id) from exc
        except ValueError as exc:
            raise OperationError.validation_error(str(exc)) from exc
        return RecoverableTimeoutDecisionResponse(incident_id=req.incident_id, decision="extend")


class AbortRecoverableTimeoutOperation:
    Request: ClassVar[type[BaseModel]] = ABORT_RECOVERABLE_TIMEOUT_ROUTE.request
    Response: ClassVar[type[BaseModel]] = ABORT_RECOVERABLE_TIMEOUT_ROUTE.response
    binding: ClassVar[RestBinding] = ABORT_RECOVERABLE_TIMEOUT_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(
        self, req: AbortRecoverableTimeoutRequest,
    ) -> RecoverableTimeoutDecisionResponse:
        try:
            await self._runtime.incidents.recoverable_timeout_abort(
                req.incident_id, req.operator_name, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise _no_held_command(req.incident_id) from exc
        except ValueError as exc:
            raise OperationError.validation_error(str(exc)) from exc
        return RecoverableTimeoutDecisionResponse(incident_id=req.incident_id, decision="abort")


class MarkRecoverableTimeoutCompleteOperation:
    Request: ClassVar[type[BaseModel]] = MARK_RECOVERABLE_TIMEOUT_COMPLETE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = MARK_RECOVERABLE_TIMEOUT_COMPLETE_ROUTE.response
    binding: ClassVar[RestBinding] = MARK_RECOVERABLE_TIMEOUT_COMPLETE_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(
        self, req: MarkRecoverableTimeoutCompleteRequest,
    ) -> RecoverableTimeoutDecisionResponse:
        try:
            left_as_it_is = await self._runtime.incidents.recoverable_timeout_mark_complete(
                req.incident_id, req.operator_name, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise _no_held_command(req.incident_id) from exc
        except SlotOccupiedError as exc:
            raise slot_occupied(exc) from exc
        except ValueError as exc:
            raise OperationError.validation_error(str(exc)) from exc
        return RecoverableTimeoutDecisionResponse(
            incident_id=req.incident_id, decision="mark_complete", record_left_as_it_is=left_as_it_is,
        )


INCIDENT_OPERATIONS = (
    ListIncidentsOperation,
    GetIncidentOperation,
    AcknowledgeIncidentOperation,
    AcknowledgeAllIncidentsOperation,
    ExtendRecoverableTimeoutOperation,
    AbortRecoverableTimeoutOperation,
    MarkRecoverableTimeoutCompleteOperation,
)
