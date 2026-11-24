from orca.operations.execution_models import (
    CLOSE_EXECUTION_ROUTE,
    GET_EXECUTION_DETAIL_ROUTE,
    LIST_EXECUTIONS_ROUTE,
    REMOVE_EXECUTION_ROUTE,
    SET_EXECUTION_SIM_SPEEDUP_ROUTE,
    STOP_EXECUTION_ROUTE,
)
"""Execution-lifecycle Operations.

`CloseExecutionOperation` transitions an ACCEPTING execution to
DRAINING; live threads keep running, but JOIN_EXISTING submissions
for the same workflow start a fresh execution.

`ExecutionReader` answers what one execution is: the live runtime's
detail, else the stored record a restart leaves. The detail read and
the thread list both answer through it, and the list builds each live
row from the same detail.

`StopExecutionOperation` stops a running execution.

`RemoveExecutionOperation` removes a terminal execution from the
live in-memory list (the persisted history is untouched).

The stored records come from the runtime's ``ExecutionRecordService``
(``runtime.execution_records``), which both servers wire over the store
they chose.
"""

from orca.operations.binding import RestBinding
from typing import ClassVar, Literal
from pydantic import BaseModel
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.runtime.execution_record import ExecutionRecord
from orca.runtime.status_models import ExecutionDetail
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.errors import SimSpeedupNotApplicableError
from orca.operations.execution_models import (
    CloseExecutionRequest,
    CloseExecutionResponse,
    ExecutionRecordModel,
    GetExecutionDetailRequest,
    GetExecutionDetailResponse,
    ListExecutionsRequest,
    ListExecutionsResponse,
    RemoveExecutionRequest,
    RemoveExecutionResponse,
    SetExecutionSimSpeedupRequest,
    SetExecutionSimSpeedupResponse,
    StopExecutionRequest,
    StopExecutionResponse,
)
from collections.abc import Awaitable, Callable, Iterable


TerminalList = Callable[[], Awaitable[Iterable[ExecutionRecord]]]


TerminalDetailLookup = Callable[[str], Awaitable[ExecutionDetail | None]]


# -- CloseExecutionOperation -------------------------------------------------


class CloseExecutionOperation:
    Request: ClassVar[type[BaseModel]] = CLOSE_EXECUTION_ROUTE.request
    Response: ClassVar[type[BaseModel]] = CLOSE_EXECUTION_ROUTE.response
    binding: ClassVar[RestBinding] = CLOSE_EXECUTION_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: CloseExecutionRequest) -> CloseExecutionResponse:
        try:
            result = self._runtime.submissions.close_execution(
                req.execution_id, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.of(
                ErrorCode.EXECUTION_NOT_FOUND,
                f"execution {req.execution_id!r} not found",
                execution_id=req.execution_id,
            ) from exc
        except RuntimeError as exc:
            raise OperationError.of(ErrorCode.CONFLICT, str(exc)) from exc
        phase = result.phase.value if hasattr(result.phase, "value") else str(result.phase)
        return CloseExecutionResponse(
            execution_id=result.execution_id, phase=phase,
        )


# The executions that ran as kept check runs.
CheckRunExecutions = Callable[[], Awaitable[set[str]]]


# -- ExecutionReader ---------------------------------------------------------


class ExecutionReader:
    """One execution's detail: the live runtime's, else the stored record a restart leaves. None when neither has it."""

    def __init__(
        self, runtime: ISystemRuntime,
        terminal_detail_lookup: TerminalDetailLookup,
        check_run_executions: CheckRunExecutions,
    ):
        self._runtime = runtime
        self._terminal_detail_lookup = terminal_detail_lookup
        self._check_run_executions = check_run_executions

    async def detail(self, execution_id: str) -> GetExecutionDetailResponse | None:
        try:
            detail: ExecutionDetail | None = self._runtime.get_execution_detail(execution_id)
        except KeyError:
            detail = await self._terminal_detail_lookup(execution_id)
        if detail is None:
            return None
        check_run = execution_id in await self._check_run_executions()
        return GetExecutionDetailResponse.from_detail(detail, check_run=check_run)


# -- ListExecutionsOperation -------------------------------------------------


class ListExecutionsOperation:
    Request: ClassVar[type[BaseModel]] = LIST_EXECUTIONS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_EXECUTIONS_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_EXECUTIONS_ROUTE.binding

    def __init__(
        self, runtime: ISystemRuntime,
        terminal_list: TerminalList,
        check_run_executions: CheckRunExecutions,
    ):
        self._runtime = runtime
        self._terminal_list = terminal_list
        self._check_run_executions = check_run_executions

    async def run(self, req: ListExecutionsRequest) -> ListExecutionsResponse:
        del req
        execs: list[ExecutionRecordModel] = []
        live_ids: set[str] = set()
        for exe in self._runtime.iter_executions():
            try:
                detail = self._runtime.get_execution_detail(exe.id)
            except KeyError:
                # A live run whose labware was cleared by force has no position to report: its stored record is listed.
                continue
            execs.append(GetExecutionDetailResponse.from_detail(detail).record())
            live_ids.add(exe.id)
        for record in await self._terminal_list():
            if record.id in live_ids:
                continue
            execs.append(ExecutionRecordModel.from_record(record))
        check_runs = await self._check_run_executions()
        return ListExecutionsResponse(executions=[
            e.model_copy(update={"check_run": True}) if e.id in check_runs else e for e in execs
        ])


# -- GetExecutionDetailOperation ---------------------------------------------


class GetExecutionDetailOperation:
    Request: ClassVar[type[BaseModel]] = GET_EXECUTION_DETAIL_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_EXECUTION_DETAIL_ROUTE.response
    binding: ClassVar[RestBinding] = GET_EXECUTION_DETAIL_ROUTE.binding

    def __init__(self, reader: ExecutionReader):
        self._reader = reader

    async def run(self, req: GetExecutionDetailRequest) -> GetExecutionDetailResponse:
        detail = await self._reader.detail(req.execution_id)
        if detail is None:
            raise OperationError.of(
                ErrorCode.EXECUTION_NOT_FOUND,
                f"execution {req.execution_id!r} not found",
                execution_id=req.execution_id,
            )
        return detail


# -- StopExecutionOperation --------------------------------------------------


class StopExecutionOperation:
    Request: ClassVar[type[BaseModel]] = STOP_EXECUTION_ROUTE.request
    Response: ClassVar[type[BaseModel]] = STOP_EXECUTION_ROUTE.response
    binding: ClassVar[RestBinding] = STOP_EXECUTION_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: StopExecutionRequest) -> StopExecutionResponse:
        try:
            outcome = await self._runtime.submissions.stop_execution(
                req.execution_id, abort=req.confirm, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.of(
                ErrorCode.EXECUTION_NOT_FOUND,
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


# -- SetExecutionSimSpeedupOperation ------------------------------------------


def _sim_speedup_message(speedup: float) -> str:
    if speedup == 1:
        return "real time"
    if speedup > 1:
        return f"{speedup:g}x faster than real time"
    return f"{1 / speedup:g}x slower than real time"


class SetExecutionSimSpeedupOperation:
    Request: ClassVar[type[BaseModel]] = SET_EXECUTION_SIM_SPEEDUP_ROUTE.request
    Response: ClassVar[type[BaseModel]] = SET_EXECUTION_SIM_SPEEDUP_ROUTE.response
    binding: ClassVar[RestBinding] = SET_EXECUTION_SIM_SPEEDUP_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: SetExecutionSimSpeedupRequest) -> SetExecutionSimSpeedupResponse:
        try:
            speedup = self._runtime.set_execution_sim_speedup(
                req.execution_id, req.sim_speedup,
            )
        except KeyError as exc:
            raise OperationError.of(
                ErrorCode.EXECUTION_NOT_FOUND,
                f"execution {req.execution_id!r} not found",
                execution_id=req.execution_id,
            ) from exc
        except SimSpeedupNotApplicableError as exc:
            raise OperationError.of(
                ErrorCode.SIM_SPEEDUP_NOT_APPLICABLE,
                str(exc),
                reason=exc.reason,
                execution_id=req.execution_id,
            ) from exc
        except ValueError as exc:
            raise OperationError.validation_error(str(exc)) from exc
        except RuntimeError as exc:
            raise OperationError.of(
                ErrorCode.CONFLICT,
                str(exc), execution_id=req.execution_id,
            ) from exc
        return SetExecutionSimSpeedupResponse(
            execution_id=req.execution_id,
            sim_speedup=speedup,
            message=_sim_speedup_message(speedup),
        )


# -- RemoveExecutionOperation ------------------------------------------------


class RemoveExecutionOperation:
    Request: ClassVar[type[BaseModel]] = REMOVE_EXECUTION_ROUTE.request
    Response: ClassVar[type[BaseModel]] = REMOVE_EXECUTION_ROUTE.response
    binding: ClassVar[RestBinding] = REMOVE_EXECUTION_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: RemoveExecutionRequest) -> RemoveExecutionResponse:
        try:
            self._runtime.submissions.remove_execution(
                req.execution_id, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.of(
                ErrorCode.EXECUTION_NOT_FOUND,
                f"execution {req.execution_id!r} not found",
                execution_id=req.execution_id,
            ) from exc
        except (ValueError, RuntimeError) as exc:
            raise OperationError.of(
                ErrorCode.CONFLICT,
                str(exc), execution_id=req.execution_id,
            ) from exc
        return RemoveExecutionResponse(execution_id=req.execution_id)


EXECUTION_CONTROL_OPERATIONS = (
    CloseExecutionOperation,
    StopExecutionOperation,
    SetExecutionSimSpeedupOperation,
    RemoveExecutionOperation,
)
