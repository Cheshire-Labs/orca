from orca.operations.variable_models import (
    GET_GLOBAL_VARIABLE_ROUTE,
    GET_VARIABLE_ROUTE,
    LIST_ALL_VARIABLES_ROUTE,
    LIST_VARIABLES_ROUTE,
    SET_GLOBAL_VARIABLE_ROUTE,
    SET_SUBMISSION_VARIABLE_ROUTE,
    SET_VARIABLE_ROUTE,
    UNSET_GLOBAL_VARIABLE_ROUTE,
    UNSET_SUBMISSION_VARIABLE_ROUTE,
    UNSET_VARIABLE_ROUTE,
)
"""Read and write workflow variables: global, per execution, and per submission.

Writes are operator overrides, so each one passes `confirm=True` to the facade.
"""

from typing import ClassVar

from pydantic import BaseModel

from orca.operations.binding import RestBinding
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.operations.variable_models import (
    CrossExecVariableDTO,
    GetGlobalVariableRequest,
    GetVariableRequest,
    ListAllVariablesRequest,
    ListVariablesRequest,
    SetGlobalVariableRequest,
    SetSubmissionVariableRequest,
    SetVariableRequest,
    UnsetGlobalVariableRequest,
    UnsetSubmissionVariableRequest,
    UnsetVariableRequest,
    SubmissionOverrideDTO,
    VariableRowsResponse,
    VariableSetResponse,
    VariablesListResponse,
    VariableUnsetResponse,
    VariableValue,
)
from orca.runtime.runtime_interface import ISystemRuntime
from orca.variables.errors import UndefinedVariableError, VariableValidationError
from orca.variables.resolution import VariableSource


def _execution_not_found(execution_id: str) -> OperationError:
    return OperationError.of(
        ErrorCode.EXECUTION_NOT_FOUND, f"execution {execution_id!r} not found", execution_id=execution_id,
    )


def _variable_not_found(name: str, execution_id: str | None) -> OperationError:
    where = "globally" if execution_id is None else f"on {execution_id!r}"
    return OperationError.of(
        ErrorCode.VARIABLE_NOT_FOUND, f"variable {name!r} not found {where}", name=name,
    )


def _is_live(runtime: ISystemRuntime, execution_id: str) -> bool:
    try:
        runtime.get_execution(execution_id)
    except KeyError:
        return False
    return True


async def _write_refused(runtime: ISystemRuntime, execution_id: str) -> OperationError:
    """A write the variable store refused: the execution ended, or never existed.

    An ended execution is a 409, because its id is real and only its variables
    were torn down. Telling that apart from a typo saves the operator a hunt.
    """
    if _is_live(runtime, execution_id) or await runtime.execution_records.contains(execution_id):
        return OperationError.of(
            ErrorCode.EXECUTION_TERMINAL,
            f"execution {execution_id!r} has ended; its variables can no longer change", execution_id=execution_id,
        )
    return _execution_not_found(execution_id)


class GetGlobalVariableOperation:
    Request: ClassVar[type[BaseModel]] = GET_GLOBAL_VARIABLE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_GLOBAL_VARIABLE_ROUTE.response
    binding: ClassVar[RestBinding] = GET_GLOBAL_VARIABLE_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: GetGlobalVariableRequest) -> VariableValue:
        variables = self._runtime.variables
        try:
            value = variables.get_global(req.name)
        except UndefinedVariableError as exc:
            raise _variable_not_found(req.name, None) from exc
        source = (
            VariableSource.GLOBAL if variables.has_global_value(req.name)
            else VariableSource.GLOBAL_DEFAULT
        )
        return VariableValue(name=req.name, value=value, source=source)


class SetGlobalVariableOperation:
    Request: ClassVar[type[BaseModel]] = SET_GLOBAL_VARIABLE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = SET_GLOBAL_VARIABLE_ROUTE.response
    binding: ClassVar[RestBinding] = SET_GLOBAL_VARIABLE_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: SetGlobalVariableRequest) -> VariableSetResponse:
        try:
            await self._runtime.variables.set_global(req.name, req.value, reason=req.reason, confirm=True)
        except VariableValidationError as exc:
            raise OperationError.validation_error(str(exc)) from exc
        return VariableSetResponse(name=req.name, value=req.value)


class UnsetGlobalVariableOperation:
    Request: ClassVar[type[BaseModel]] = UNSET_GLOBAL_VARIABLE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = UNSET_GLOBAL_VARIABLE_ROUTE.response
    binding: ClassVar[RestBinding] = UNSET_GLOBAL_VARIABLE_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: UnsetGlobalVariableRequest) -> VariableUnsetResponse:
        variables = self._runtime.variables
        existed = variables.has_global_value(req.name)
        await variables.unset_global(req.name, reason=req.reason, confirm=True)
        return VariableUnsetResponse(existed=existed)


class ListAllVariablesOperation:
    """Every variable of every execution the runtime holds.

    An execution removed between listing and reading is skipped: reading it
    would fall through to the global view and file global rows under a dead id.
    """

    Request: ClassVar[type[BaseModel]] = LIST_ALL_VARIABLES_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_ALL_VARIABLES_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_ALL_VARIABLES_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: ListAllVariablesRequest) -> VariableRowsResponse:
        rows: list[CrossExecVariableDTO] = []
        for execution in self._runtime.iter_executions():
            if not _is_live(self._runtime, execution.id):
                continue
            for name, value in self._runtime.variables.get_all(execution.id).items():
                rows.append(CrossExecVariableDTO(execution_id=execution.id, name=name, value=value))
        return VariableRowsResponse(rows)


class ListVariablesOperation:
    """One execution's merged view.

    The facade falls through to the global view for an unknown id, so the id
    is checked first; otherwise a typo reads as "no variables".
    """

    Request: ClassVar[type[BaseModel]] = LIST_VARIABLES_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_VARIABLES_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_VARIABLES_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: ListVariablesRequest) -> VariablesListResponse:
        if not _is_live(self._runtime, req.execution_id):
            raise _execution_not_found(req.execution_id)
        return VariablesListResponse(self._runtime.variables.get_all(req.execution_id))


class GetVariableOperation:
    """What a thread resolves for one name.

    Without a submission this is what a submission with no override resolves,
    and `overrides` lists the ones that resolve something else, with their values.
    """

    Request: ClassVar[type[BaseModel]] = GET_VARIABLE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_VARIABLE_ROUTE.response
    binding: ClassVar[RestBinding] = GET_VARIABLE_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: GetVariableRequest) -> VariableValue:
        try:
            resolution = self._runtime.variables.explain(req.name, req.execution_id)
        except KeyError as exc:
            raise _execution_not_found(req.execution_id) from exc
        if req.submission_id is not None:
            binding = resolution.for_submission(req.submission_id)
            if binding is None:
                raise _variable_not_found(req.name, req.execution_id)
            return VariableValue(
                name=req.name, execution_id=req.execution_id,
                value=binding.value, source=binding.source,
            )
        if resolution.source is None and not resolution.overrides:
            raise _variable_not_found(req.name, req.execution_id)
        return VariableValue(
            name=req.name, execution_id=req.execution_id,
            value=resolution.value, source=resolution.source,
            overrides=[SubmissionOverrideDTO(submission_id=o.submission_id, value=o.value) for o in resolution.overrides],
        )


class SetVariableOperation:
    Request: ClassVar[type[BaseModel]] = SET_VARIABLE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = SET_VARIABLE_ROUTE.response
    binding: ClassVar[RestBinding] = SET_VARIABLE_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: SetVariableRequest) -> VariableSetResponse:
        variables = self._runtime.variables
        try:
            variables.set(req.name, req.value, req.execution_id, reason=req.reason, confirm=True)
            resolution = variables.explain(req.name, req.execution_id)
        except KeyError as exc:
            raise await _write_refused(self._runtime, req.execution_id) from exc
        except VariableValidationError as exc:
            raise OperationError.validation_error(str(exc)) from exc
        return VariableSetResponse(
            name=req.name, value=req.value,
            shadowed_by=[o.submission_id for o in resolution.overrides],
        )


class UnsetVariableOperation:
    Request: ClassVar[type[BaseModel]] = UNSET_VARIABLE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = UNSET_VARIABLE_ROUTE.response
    binding: ClassVar[RestBinding] = UNSET_VARIABLE_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: UnsetVariableRequest) -> VariableUnsetResponse:
        variables = self._runtime.variables
        try:
            existed = variables.has_execution_value(req.name, req.execution_id)
            variables.unset(req.name, req.execution_id, reason=req.reason, confirm=True)
        except KeyError as exc:
            raise await _write_refused(self._runtime, req.execution_id) from exc
        return VariableUnsetResponse(existed=existed)


class SetSubmissionVariableOperation:
    """Write the layer that outranks the execution's, for one submission only."""

    Request: ClassVar[type[BaseModel]] = SET_SUBMISSION_VARIABLE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = SET_SUBMISSION_VARIABLE_ROUTE.response
    binding: ClassVar[RestBinding] = SET_SUBMISSION_VARIABLE_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: SetSubmissionVariableRequest) -> VariableSetResponse:
        try:
            self._runtime.variables.set_submission(
                req.name, req.value, req.execution_id, req.submission_id,
                reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise await _write_refused(self._runtime, req.execution_id) from exc
        except VariableValidationError as exc:
            raise OperationError.validation_error(str(exc)) from exc
        return VariableSetResponse(name=req.name, value=req.value)


class UnsetSubmissionVariableOperation:
    Request: ClassVar[type[BaseModel]] = UNSET_SUBMISSION_VARIABLE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = UNSET_SUBMISSION_VARIABLE_ROUTE.response
    binding: ClassVar[RestBinding] = UNSET_SUBMISSION_VARIABLE_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime) -> None:
        self._runtime = runtime

    async def run(self, req: UnsetSubmissionVariableRequest) -> VariableUnsetResponse:
        variables = self._runtime.variables
        try:
            existed = variables.has_submission_value(req.name, req.execution_id, req.submission_id)
            variables.unset_submission(
                req.name, req.execution_id, req.submission_id, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise await _write_refused(self._runtime, req.execution_id) from exc
        return VariableUnsetResponse(existed=existed)


# Static segments (`global`, `submissions`) are bound before the routes whose
# first segment is an execution id, so a router matches them first.
VARIABLE_OPERATIONS = (
    GetGlobalVariableOperation,
    SetGlobalVariableOperation,
    UnsetGlobalVariableOperation,
    SetSubmissionVariableOperation,
    UnsetSubmissionVariableOperation,
    ListAllVariablesOperation,
    ListVariablesOperation,
    GetVariableOperation,
    SetVariableOperation,
    UnsetVariableOperation,
)
