"""Operations over a run's threads and reservations, and the loaded plugins."""

from typing import ClassVar

from pydantic import BaseModel

from orca.operations.wire_models import (
    PluginDTO,
    ReservationCancelResponse,
    ReservationSnapshotDTO,
    ThreadSnapshotResponse,
)
from orca.operations.binding import RestBinding
from orca.operations.execution import ExecutionReader
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.runtime.runtime_interface import ISystemRuntime
from orca.operations.runtime_read_models import (
    CANCEL_RESERVATION_ROUTE,
    GET_THREAD_ROUTE,
    LIST_RESERVATIONS_ROUTE,
    LIST_PLUGINS_ROUTE,
    LIST_RESERVATIONS_ROUTE,
)
from orca.operations.runtime_read_models import (
    CancelReservationRequest,
    GetThreadRequest,
    ListPluginsRequest,
    ListReservationsRequest,
    PluginListResponse,
    ReservationListResponse,
)


class GetThreadOperation:
    """One thread of a run, answered from the stored record once a restart has emptied the live runtime."""

    Request: ClassVar[type[BaseModel]] = GET_THREAD_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_THREAD_ROUTE.response
    binding: ClassVar[RestBinding] = GET_THREAD_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime, reader: ExecutionReader) -> None:
        self._runtime = runtime
        self._reader = reader

    async def run(self, req: GetThreadRequest) -> ThreadSnapshotResponse:
        try:
            return ThreadSnapshotResponse.from_dc(self._runtime.threads.get(req.execution_id, req.thread_id))
        except KeyError:
            pass
        detail = await self._reader.detail(req.execution_id)
        stored = [] if detail is None else [t for t in detail.threads if t.id == req.thread_id]
        if not stored:
            raise OperationError.of(ErrorCode.THREAD_NOT_FOUND, f"thread {req.thread_id!r} not found")
        return stored[0]


class ListReservationsOperation:
    Request: ClassVar[type[BaseModel]] = LIST_RESERVATIONS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_RESERVATIONS_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_RESERVATIONS_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: ListReservationsRequest) -> ReservationListResponse:
        if req.execution_id is not None:
            try:
                return ReservationListResponse(self._rows_of(req.execution_id))
            except KeyError as exc:
                raise OperationError(
                    ErrorCode.EXECUTION_NOT_FOUND,
                    f"execution {req.execution_id!r} not found",
                ) from exc
        rows: list[ReservationSnapshotDTO] = []
        for execution in self._runtime.iter_executions():
            try:
                rows.extend(self._rows_of(execution.id))
            except KeyError:
                continue
        return ReservationListResponse(rows)

    def _rows_of(self, execution_id: str) -> list[ReservationSnapshotDTO]:
        return [
            ReservationSnapshotDTO.from_dc(s).model_copy(update={"execution_id": execution_id})
            for s in self._runtime.registry.list_reservations(execution_id)
        ]


class CancelReservationOperation:
    Request: ClassVar[type[BaseModel]] = CANCEL_RESERVATION_ROUTE.request
    Response: ClassVar[type[BaseModel]] = CANCEL_RESERVATION_ROUTE.response
    binding: ClassVar[RestBinding] = CANCEL_RESERVATION_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: CancelReservationRequest) -> ReservationCancelResponse:
        if not req.reason.strip():
            raise OperationError(
                ErrorCode.VALIDATION_ERROR,
                "reason is required for reservation cancel",
            )
        try:
            await self._runtime.registry.cancel_reservation(
                req.execution_id, req.reservation_id, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.of(
                ErrorCode.RESERVATION_NOT_FOUND,
                f"reservation {req.reservation_id!r} not found",
            ) from exc
        except ValueError as exc:
            raise OperationError(
                ErrorCode.DANGEROUS_CHECK_FAILED, str(exc),
            ) from exc
        return ReservationCancelResponse()


class ListPluginsOperation:
    Request: ClassVar[type[BaseModel]] = LIST_PLUGINS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_PLUGINS_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_PLUGINS_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: ListPluginsRequest) -> PluginListResponse:
        del req
        return PluginListResponse([PluginDTO.from_dc(p) for p in self._runtime.list_plugins()])


RUNTIME_READ_OPERATIONS = (
    ListReservationsOperation,
    CancelReservationOperation,
    ListPluginsOperation,
)
