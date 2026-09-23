"""Execution-lifecycle Operations.

`CloseExecutionOperation` transitions an ACCEPTING execution to
DRAINING; live threads keep running, but JOIN_EXISTING submissions
for the same workflow start a fresh execution.

`GetExecutionOperation` returns the live ExecutionRecord (id +
workflow + status + error) for a single execution. Accepts an
optional ``terminal_lookup`` callable so a deployment that persists
terminal executions can rehydrate records the live runtime no longer
holds (Bug SS).

`ListExecutionsOperation` returns every live execution's record;
the optional ``terminal_list`` callable merges DB-backed terminal
records into the response so a rebuild + list survives Bug SS too.

`GetExecutionDetailOperation` returns the rich snapshot
(`ExecutionDetail`) including thread snapshots; ``terminal_detail_lookup``
falls back to an empty-threads ExecutionDetail rehydrated from the
DB row when the runtime no longer holds the execution.

`StopExecutionOperation` stops a running execution.

`RemoveExecutionOperation` removes a terminal execution from the
live in-memory list (the persisted history is untouched).

The terminal-lookup callables come from the runtime's
``ExecutionRecordService`` (``runtime.execution_records``), which both
the orca-core daemon (over its SQLite store) and a hosted deployment
(over its injected store) wire. A deployment with no execution-record
service passes ``None``.
"""

from typing import ClassVar, Literal
from pydantic import BaseModel
from orca.operations._protocol import OperationError
from orca.runtime.execution_phase import ExecutionPhase
from orca.runtime.execution_record import ExecutionRecord, ExecutionState
from orca.runtime.status_models import ExecutionDetail
from orca.runtime.runtime_interface import ISystemRuntime
from orca.operations.execution_models import (
    CloseExecutionRequest,
    CloseExecutionResponse,
    ExecutionRecordModel,
    GetExecutionDetailRequest,
    GetExecutionDetailResponse,
    GetExecutionRequest,
    GetExecutionResponse,
    ListExecutionsRequest,
    ListExecutionsResponse,
    RemoveExecutionRequest,
    RemoveExecutionResponse,
    StopExecutionRequest,
    StopExecutionResponse,
)
from collections.abc import Awaitable, Callable, Iterable


# Deployment-injected callables for sink-fallback rehydration. None on
# deployments without a DB-backed terminal-record store (the daemon).
TerminalLookup = Callable[[str], Awaitable["ExecutionRecord | None"]]


TerminalList = Callable[[], Awaitable[Iterable["ExecutionRecord"]]]


TerminalDetailLookup = Callable[[str], Awaitable["ExecutionDetail | None"]]


# -- CloseExecutionOperation -------------------------------------------------


class CloseExecutionOperation:
    Request: ClassVar[type[BaseModel]] = CloseExecutionRequest
    Response: ClassVar[type[BaseModel]] = CloseExecutionResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: CloseExecutionRequest) -> CloseExecutionResponse:
        try:
            result = self._runtime.submissions.close_execution(
                req.execution_id, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"execution {req.execution_id!r} not found",
                execution_id=req.execution_id,
            ) from exc
        except RuntimeError as exc:
            raise OperationError.conflict(str(exc)) from exc
        phase = result.phase.value if hasattr(result.phase, "value") else str(result.phase)
        return CloseExecutionResponse(
            execution_id=result.execution_id, phase=phase,
        )


# -- GetExecutionOperation ---------------------------------------------------


class GetExecutionOperation:
    Request: ClassVar[type[BaseModel]] = GetExecutionRequest
    Response: ClassVar[type[BaseModel]] = GetExecutionResponse

    def __init__(
        self, runtime: ISystemRuntime,
        terminal_lookup: TerminalLookup | None = None,
    ):
        self._runtime = runtime
        self._terminal_lookup = terminal_lookup

    async def run(self, req: GetExecutionRequest) -> GetExecutionResponse:
        try:
            status = self._runtime.get_execution_status(req.execution_id)
            return GetExecutionResponse(
                execution=ExecutionRecordModel.from_status(status),
            )
        except KeyError:
            pass
        if self._terminal_lookup is not None:
            record = await self._terminal_lookup(req.execution_id)
            if record is not None:
                return GetExecutionResponse(
                    execution=ExecutionRecordModel.from_record(record),
                )
        raise OperationError.not_found(
            f"execution {req.execution_id!r} not found",
            execution_id=req.execution_id,
        )


# -- ListExecutionsOperation -------------------------------------------------


class ListExecutionsOperation:
    Request: ClassVar[type[BaseModel]] = ListExecutionsRequest
    Response: ClassVar[type[BaseModel]] = ListExecutionsResponse

    def __init__(
        self, runtime: ISystemRuntime,
        terminal_list: TerminalList | None = None,
    ):
        self._runtime = runtime
        self._terminal_list = terminal_list

    async def run(self, req: ListExecutionsRequest) -> ListExecutionsResponse:
        del req
        execs: list[ExecutionRecordModel] = []
        live_ids: set[str] = set()
        for exe in self._runtime.iter_executions():
            phase: ExecutionPhase | ExecutionState = exe.phase
            execs.append(ExecutionRecordModel(
                id=exe.id,
                workflow_name=exe.workflow_name,
                status=phase.value if hasattr(phase, "value") else str(phase),
                error=exe.error,
                paused=exe.is_paused,
                pause_reason=exe.pause_reason,
                abort_armed=exe.abort_armed,
            ))
            live_ids.add(exe.id)
        if self._terminal_list is not None:
            for record in await self._terminal_list():
                if record.id in live_ids:
                    continue
                execs.append(ExecutionRecordModel.from_record(record))
        return ListExecutionsResponse(executions=execs)


# -- GetExecutionDetailOperation ---------------------------------------------


class GetExecutionDetailOperation:
    Request: ClassVar[type[BaseModel]] = GetExecutionDetailRequest
    Response: ClassVar[type[BaseModel]] = GetExecutionDetailResponse

    def __init__(
        self, runtime: ISystemRuntime,
        terminal_detail_lookup: TerminalDetailLookup | None = None,
    ):
        self._runtime = runtime
        self._terminal_detail_lookup = terminal_detail_lookup

    async def run(self, req: GetExecutionDetailRequest) -> GetExecutionDetailResponse:
        try:
            detail = self._runtime.get_execution_detail(req.execution_id)
            return GetExecutionDetailResponse.from_detail(detail)
        except KeyError:
            pass
        if self._terminal_detail_lookup is not None:
            detail = await self._terminal_detail_lookup(req.execution_id)
            if detail is not None:
                return GetExecutionDetailResponse.from_detail(detail)
        raise OperationError.not_found(
            f"execution {req.execution_id!r} not found",
            execution_id=req.execution_id,
        )


# -- StopExecutionOperation --------------------------------------------------


class StopExecutionOperation:
    Request: ClassVar[type[BaseModel]] = StopExecutionRequest
    Response: ClassVar[type[BaseModel]] = StopExecutionResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: StopExecutionRequest) -> StopExecutionResponse:
        try:
            outcome = await self._runtime.stop_execution(
                req.execution_id, confirm=req.confirm,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"execution {req.execution_id!r} not found",
                execution_id=req.execution_id,
            ) from exc
        status: Literal["armed", "aborted", "already_terminal"]
        if outcome.aborted:
            status = "aborted"
            message = f"execution {req.execution_id} aborted"
        elif outcome.armed:
            status = "armed"
            message = (
                "execution paused and abort armed; call stop again with "
                "confirm=true to abort, or resume to disarm"
            )
        else:
            status = "already_terminal"
            message = (
                f"execution {req.execution_id} already {outcome.phase.value}; "
                f"nothing to stop"
            )
        return StopExecutionResponse(
            status=status,
            execution_id=req.execution_id,
            phase=outcome.phase.value,
            message=message,
        )


# -- RemoveExecutionOperation ------------------------------------------------


class RemoveExecutionOperation:
    Request: ClassVar[type[BaseModel]] = RemoveExecutionRequest
    Response: ClassVar[type[BaseModel]] = RemoveExecutionResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: RemoveExecutionRequest) -> RemoveExecutionResponse:
        try:
            self._runtime.remove_execution(req.execution_id, confirm=True)
        except KeyError as exc:
            raise OperationError.not_found(
                f"execution {req.execution_id!r} not found",
                execution_id=req.execution_id,
            ) from exc
        except (ValueError, RuntimeError) as exc:
            raise OperationError.conflict(
                str(exc), execution_id=req.execution_id,
            ) from exc
        return RemoveExecutionResponse(execution_id=req.execution_id)
