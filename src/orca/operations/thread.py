from orca.operations.thread_models import (
    INSERT_ACTION_ROUTE,
    INSERT_METHOD_ROUTE,
    PAUSE_ROUTE,
    RECOVER_THREAD_ROUTE,
    REPLACE_ACTION_ROUTE,
    REPLACE_METHOD_ROUTE,
    RESUME_ROUTE,
    SKIP_ACTION_ROUTE,
    SKIP_METHOD_ROUTE,
    SPAWN_THREAD_ROUTE,
)
"""Thread-mutation Operations.

Exercises the `Scope` discriminated union (ExecutionScope | ThreadScope)
to collapse two-route pairs (`pause_execution` /
`pause_thread`, `resume_execution` / `resume_thread`) into a single
`Pause` / `Resume` Operation. The Operation's run() dispatches on
`scope.kind` and calls the right facade method.

`SpawnThreadOperation` and `RecoverThreadOperation` are single-target
Operations (no scope dispatch; the wire shape carries the execution_id
and thread_id directly).

Skip, abort and insert for methods and actions are Operations too, further
down this module.
"""

from orca.operations.binding import RestBinding
from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar
from pydantic import BaseModel
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError, message_of
from orca.runtime.errors import ExecutionNotFoundError, LabwareNotFoundError, ThreadTemplateNotFoundError
from orca.operations.scope import ExecutionScope, Scope
from orca.runtime.code_injection import (
    CodeInjectionError,
    CodeValidationError,
    LiveTopology,
    compile_action_code,
    compile_method_code,
)
from orca.runtime.runtime_interface import ISystemRuntime
from orca.workflow_models.status_enums import RecoveryDecision
from orca.operations.thread_models import (
    InsertActionRequest,
    InsertMethodRequest,
    InsertWhere,
    PauseRequest,
    PauseResponse,
    RecoverThreadRequest,
    RecoverThreadResponse,
    ReplaceActionRequest,
    ReplaceMethodRequest,
    ReplaceResult,
    ResumeRequest,
    ResumeResponse,
    SkipActionRequest,
    SkipMethodRequest,
    SpawnThreadRequest,
    SpawnThreadResponse,
    _PauseExecutionResult,
    _PauseThreadResult,
    ResumeExecutionResult,
    _ResumeThreadResult,
    ThreadMutationResult,
)
from orca.workflow_models.mutation_position import (
    After,
    AtHead,
    AtTail,
    Before,
    InsertPosition,
)


# -- Pause Operation ---------------------------------------------------------


class PauseOperation:
    Request: ClassVar[type[BaseModel]] = PAUSE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = PAUSE_ROUTE.response
    binding: ClassVar[RestBinding] = PAUSE_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: PauseRequest) -> PauseResponse:
        scope = req.scope
        try:
            if isinstance(scope, ExecutionScope):
                result = await self._runtime.threads.pause_all(
                    scope.execution_id, reason=req.reason, confirm=True,
                )
                return PauseResponse(result=_PauseExecutionResult(
                    pausing=int(result.get("pausing", 0)),
                    already_paused=int(result.get("already_paused", 0)),
                    terminal_skipped=int(result.get("terminal_skipped", 0)),
                ))
            await self._runtime.threads.pause(
                scope.execution_id, scope.thread_id,
                reason=req.reason, confirm=True,
            )
            return PauseResponse(result=_PauseThreadResult(
                execution_id=scope.execution_id,
                thread_id=scope.thread_id,
            ))
        except KeyError as exc:
            raise _scope_not_found(scope) from exc


# -- Resume Operation --------------------------------------------------------


class ResumeOperation:
    Request: ClassVar[type[BaseModel]] = RESUME_ROUTE.request
    Response: ClassVar[type[BaseModel]] = RESUME_ROUTE.response
    binding: ClassVar[RestBinding] = RESUME_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: ResumeRequest) -> ResumeResponse:
        scope = req.scope
        try:
            if isinstance(scope, ExecutionScope):
                result = await self._runtime.threads.resume_all(
                    scope.execution_id, reason=req.reason, confirm=True,
                )
                return ResumeResponse(result=ResumeExecutionResult(
                    resumed=int(result.get("resumed", 0)),
                    pause_cancelled=int(result.get("pause_cancelled", 0)),
                    error_skipped=int(result.get("error_skipped", 0)),
                    completed_skipped=int(result.get("completed_skipped", 0)),
                ))
            await self._runtime.threads.resume(
                scope.execution_id, scope.thread_id,
                reason=req.reason, confirm=True,
            )
            return ResumeResponse(result=_ResumeThreadResult(
                execution_id=scope.execution_id,
                thread_id=scope.thread_id,
            ))
        except KeyError as exc:
            raise _scope_not_found(scope) from exc
        except ValueError as exc:
            raise OperationError.of(ErrorCode.CONFLICT, str(exc)) from exc


# -- SpawnThread Operation ---------------------------------------------------


class SpawnThreadOperation:
    Request: ClassVar[type[BaseModel]] = SPAWN_THREAD_ROUTE.request
    Response: ClassVar[type[BaseModel]] = SPAWN_THREAD_ROUTE.response
    binding: ClassVar[RestBinding] = SPAWN_THREAD_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: SpawnThreadRequest) -> SpawnThreadResponse:
        try:
            snap = await self._runtime.threads.spawn_thread(
                req.execution_id, req.template_name,
                labware_id=req.labware_id, reason=req.reason, confirm=True,
            )
        except ThreadTemplateNotFoundError as exc:
            raise OperationError.of(
                ErrorCode.THREAD_TEMPLATE_NOT_FOUND, message_of(exc), template_name=req.template_name,
            ) from exc
        except LabwareNotFoundError as exc:
            raise OperationError.of(ErrorCode.LABWARE_NOT_FOUND, message_of(exc), labware_id=exc.labware_id) from exc
        except ExecutionNotFoundError as exc:
            raise OperationError.of(ErrorCode.EXECUTION_NOT_FOUND, message_of(exc), execution_id=req.execution_id) from exc
        except (RuntimeError, ValueError) as exc:
            raise OperationError.of(ErrorCode.CONFLICT, str(exc)) from exc
        return SpawnThreadResponse.from_snapshot(snap)


# -- RecoverThread Operation -------------------------------------------------


class RecoverThreadOperation:
    Request: ClassVar[type[BaseModel]] = RECOVER_THREAD_ROUTE.request
    Response: ClassVar[type[BaseModel]] = RECOVER_THREAD_ROUTE.response
    binding: ClassVar[RestBinding] = RECOVER_THREAD_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: RecoverThreadRequest) -> RecoverThreadResponse:
        try:
            decision = RecoveryDecision[req.decision.upper()]
        except KeyError as exc:
            valid = ", ".join(d.name for d in RecoveryDecision)
            raise OperationError.validation_error(
                f"decision {req.decision!r} unknown; must be one of {valid}",
            ) from exc

        if decision is RecoveryDecision.ABORT_METHOD and self._manually_paused(req):
            with _thread_mutation_errors(req.execution_id, req.thread_id):
                await self._abort_the_method_in_progress(req)
            return RecoverThreadResponse(execution_id=req.execution_id, thread_id=req.thread_id)
        try:
            await self._runtime.threads.recover(
                req.execution_id, req.thread_id, decision,
                reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.of(
                ErrorCode.THREAD_NOT_FOUND,
                f"thread {req.execution_id}/{req.thread_id} not found",
                execution_id=req.execution_id,
                thread_id=req.thread_id,
            ) from exc
        except ValueError as exc:
            raise OperationError.validation_error(str(exc)) from exc
        except RuntimeError as exc:
            raise OperationError.of(ErrorCode.CONFLICT, str(exc)) from exc
        return RecoverThreadResponse(
            execution_id=req.execution_id,
            thread_id=req.thread_id,
        )

    def _manually_paused(self, req: RecoverThreadRequest) -> bool:
        try:
            snapshot = self._runtime.threads.get(req.execution_id, req.thread_id)
        except KeyError as exc:
            raise OperationError.of(
                ErrorCode.THREAD_NOT_FOUND,
                f"thread {req.execution_id}/{req.thread_id} not found",
                execution_id=req.execution_id,
                thread_id=req.thread_id,
            ) from exc
        return snapshot.pause_reason is not None and snapshot.pause_reason != "error"

    async def _abort_the_method_in_progress(self, req: RecoverThreadRequest) -> None:
        """A manually paused thread has no failed step to recover: drop the method in progress from its lane."""
        reason = _require_reason_text(req.reason)
        current = self._runtime.threads.get(req.execution_id, req.thread_id).current_method
        if current is None:
            raise RuntimeError(f"thread {req.thread_id!r} has no method in progress to abort")
        await self._runtime.threads.abort_method(
            req.execution_id, req.thread_id, method_name=current.name, reason=reason, confirm=True,
        )


def _scope_not_found(scope: Scope) -> OperationError:
    if isinstance(scope, ExecutionScope):
        return OperationError.of(ErrorCode.EXECUTION_NOT_FOUND, f"execution {scope.execution_id} not found")
    return OperationError.of(ErrorCode.THREAD_NOT_FOUND, f"thread {scope.execution_id}/{scope.thread_id} not found")


# -- Skip + abort -----------------------------------------------------------
#
# A skip names its target plus an audit reason. InsertMethod and InsertAction
# need more than that, so they have their own section below.


def _validate_one_action_selector(action_id: str | None, action_command: str | None) -> None:
    given = (action_id is not None) + (action_command is not None)
    if given == 0:
        raise OperationError.validation_error("provide exactly one of action_id or action_command")
    if given == 2:
        raise OperationError.validation_error("provide ONLY one of action_id or action_command, not both")


def _require_reason_text(reason: str | None) -> str:
    if reason is None or not reason.strip():
        raise OperationError.validation_error("reason is required and cannot be empty")
    return reason


@contextmanager
def _thread_mutation_errors(execution_id: str, thread_id: str) -> Iterator[None]:
    try:
        yield
    except KeyError as exc:
        raise OperationError.of(
            ErrorCode.THREAD_NOT_FOUND,
            message_of(exc), execution_id=execution_id, thread_id=thread_id,
        ) from exc
    except ValueError as exc:
        raise OperationError.validation_error(str(exc)) from exc
    except RuntimeError as exc:
        raise OperationError.of(ErrorCode.CONFLICT, str(exc)) from exc


# -- SkipMethodOperation ----------------------------------------------------


class SkipMethodOperation:
    Request: ClassVar[type[BaseModel]] = SKIP_METHOD_ROUTE.request
    Response: ClassVar[type[BaseModel]] = SKIP_METHOD_ROUTE.response
    binding: ClassVar[RestBinding] = SKIP_METHOD_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: SkipMethodRequest) -> ThreadMutationResult:
        reason = _require_reason_text(req.reason)
        with _thread_mutation_errors(req.execution_id, req.thread_id):
            await self._runtime.threads.skip_method(
                req.execution_id, req.thread_id,
                method_name=req.method_name,
                reason=reason, confirm=True,
            )
        return ThreadMutationResult(
            execution_id=req.execution_id, thread_id=req.thread_id,
        )


# -- SkipActionOperation ----------------------------------------------------


class SkipActionOperation:
    Request: ClassVar[type[BaseModel]] = SKIP_ACTION_ROUTE.request
    Response: ClassVar[type[BaseModel]] = SKIP_ACTION_ROUTE.response
    binding: ClassVar[RestBinding] = SKIP_ACTION_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: SkipActionRequest) -> ThreadMutationResult:
        _validate_one_action_selector(req.action_id, req.action_command)
        reason = _require_reason_text(req.reason)
        with _thread_mutation_errors(req.execution_id, req.thread_id):
            await self._runtime.threads.skip_action(
                req.execution_id, req.thread_id,
                action_id=req.action_id, action_command=req.action_command,
                reason=reason, confirm=True,
            )
        return ThreadMutationResult(
            execution_id=req.execution_id, thread_id=req.thread_id,
        )


# -- Insert method + insert action ------------------------------------------
#
# These ops compile or look up a template, validate the insert position,
# and call `threads.insert_method` / `insert_action`.


def _resolve_insert_position(
    where: InsertWhere, anchor: str | None,
) -> InsertPosition:
    """Map (where, anchor) -> concrete InsertPosition value.

    head/tail forbid anchor; before/after require it. ``where`` is constrained at the
    Pydantic request layer (``Literal["head", "tail", "before",
    "after"]``) so the runtime arm of the match below is a closed
    set; the unreachable ``else`` is kept defensive but typed
    callers never reach it.
    """
    if where == "head":
        if anchor is not None:
            raise OperationError.validation_error("anchor must be omitted for where='head'")
        return AtHead()
    if where == "tail":
        if anchor is not None:
            raise OperationError.validation_error("anchor must be omitted for where='tail'")
        return AtTail()
    if where == "before":
        if not anchor:
            raise OperationError.validation_error("anchor is required for where='before'")
        return Before(anchor_name=anchor)
    if where == "after":
        if not anchor:
            raise OperationError.validation_error("anchor is required for where='after'")
        return After(anchor_name=anchor)
    raise OperationError.validation_error(
        f"unknown where {where!r}; must be one of head/tail/before/after",
    )


class InsertMethodOperation:
    Request: ClassVar[type[BaseModel]] = INSERT_METHOD_ROUTE.request
    Response: ClassVar[type[BaseModel]] = INSERT_METHOD_ROUTE.response
    binding: ClassVar[RestBinding] = INSERT_METHOD_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: InsertMethodRequest) -> ThreadMutationResult:
        # Eager-validate cheap shape errors before any template/code work.
        reason = _require_reason_text(req.reason)
        if (req.template_name is None) == (req.method_code is None):
            raise OperationError.validation_error(
                "provide exactly one of template_name or method_code",
            )
        where = _resolve_insert_position(req.where, req.anchor)

        if req.template_name is not None:
            try:
                execution = self._runtime.get_execution(req.execution_id)
            except KeyError as exc:
                raise OperationError.of(
                    ErrorCode.EXECUTION_NOT_FOUND,
                    f"no execution {req.execution_id!r}",
                    execution_id=req.execution_id,
                ) from exc
            try:
                template = self._runtime.registry.get_method_template(
                    execution.workflow_name, req.template_name,
                )
            except KeyError as exc:
                raise OperationError.of(
                    ErrorCode.METHOD_NOT_FOUND,
                    f"no method template named {req.template_name!r} in "
                    f"workflow {execution.workflow_name!r}",
                    template_name=req.template_name,
                ) from exc
        else:
            assert req.method_code is not None
            try:
                template = compile_method_code(
                    req.method_code, LiveTopology(self._runtime.system),
                )
            except (CodeValidationError, CodeInjectionError) as exc:
                raise OperationError.validation_error(str(exc)) from exc

        with _thread_mutation_errors(req.execution_id, req.thread_id):
            await self._runtime.threads.insert_method(
                req.execution_id, req.thread_id, template, where,
                reason=reason, confirm=True,
            )
        return ThreadMutationResult(
            execution_id=req.execution_id, thread_id=req.thread_id,
        )


_STAGED_METHOD_NEXT = (
    "Replacement staged to run next. The failed method was NOT dropped. "
    "Make the cell physically safe, then call recover_thread with "
    "decision='ABORT_METHOD' to drop the failed method and run the replacement."
)


_STAGED_ACTION_NEXT = (
    "Replacement staged to run next. The failed action was NOT dropped. "
    "Make the cell physically safe, then call recover_thread with "
    "decision='ABORT_ACTION' to drop the failed action and run the replacement."
)


class ReplaceMethodOperation:
    Request: ClassVar[type[BaseModel]] = REPLACE_METHOD_ROUTE.request
    Response: ClassVar[type[BaseModel]] = REPLACE_METHOD_ROUTE.response
    binding: ClassVar[RestBinding] = REPLACE_METHOD_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: ReplaceMethodRequest) -> ReplaceResult:
        reason = _require_reason_text(req.reason)
        if (req.template_name is None) == (req.method_code is None):
            raise OperationError.validation_error(
                "provide exactly one of template_name or method_code",
            )

        if req.template_name is not None:
            try:
                execution = self._runtime.get_execution(req.execution_id)
            except KeyError as exc:
                raise OperationError.of(
                    ErrorCode.EXECUTION_NOT_FOUND,
                    f"no execution {req.execution_id!r}",
                    execution_id=req.execution_id,
                ) from exc
            try:
                template = self._runtime.registry.get_method_template(
                    execution.workflow_name, req.template_name,
                )
            except KeyError as exc:
                raise OperationError.of(
                    ErrorCode.METHOD_NOT_FOUND,
                    f"no method template named {req.template_name!r} in "
                    f"workflow {execution.workflow_name!r}",
                    template_name=req.template_name,
                ) from exc
        else:
            assert req.method_code is not None
            try:
                template = compile_method_code(
                    req.method_code, LiveTopology(self._runtime.system),
                )
            except (CodeValidationError, CodeInjectionError) as exc:
                raise OperationError.validation_error(str(exc)) from exc

        with _thread_mutation_errors(req.execution_id, req.thread_id):
            staged = await self._runtime.threads.replace_method(
                req.execution_id, req.thread_id, req.target_name, template,
                reason=reason, confirm=True,
            )
        return ReplaceResult(
            execution_id=req.execution_id, thread_id=req.thread_id,
            staged_for_recovery=staged,
            recommended_decision=RecoveryDecision.ABORT_METHOD if staged else None,
            next_step=_STAGED_METHOD_NEXT if staged else None,
        )


class InsertActionOperation:
    Request: ClassVar[type[BaseModel]] = INSERT_ACTION_ROUTE.request
    Response: ClassVar[type[BaseModel]] = INSERT_ACTION_ROUTE.response
    binding: ClassVar[RestBinding] = INSERT_ACTION_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: InsertActionRequest) -> ThreadMutationResult:
        reason = _require_reason_text(req.reason)
        where = _resolve_insert_position(req.where, req.anchor)

        try:
            action_template = compile_action_code(
                req.action_code, LiveTopology(self._runtime.system),
            )
        except (CodeValidationError, CodeInjectionError) as exc:
            raise OperationError.validation_error(str(exc)) from exc

        with _thread_mutation_errors(req.execution_id, req.thread_id):
            await self._runtime.threads.insert_action(
                req.execution_id, req.thread_id, action_template, where,
                reason=reason, confirm=True,
            )
        return ThreadMutationResult(
            execution_id=req.execution_id, thread_id=req.thread_id,
        )


class ReplaceActionOperation:
    Request: ClassVar[type[BaseModel]] = REPLACE_ACTION_ROUTE.request
    Response: ClassVar[type[BaseModel]] = REPLACE_ACTION_ROUTE.response
    binding: ClassVar[RestBinding] = REPLACE_ACTION_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: ReplaceActionRequest) -> ReplaceResult:
        reason = _require_reason_text(req.reason)
        try:
            action_template = compile_action_code(
                req.action_code, LiveTopology(self._runtime.system),
            )
        except (CodeValidationError, CodeInjectionError) as exc:
            raise OperationError.validation_error(str(exc)) from exc

        with _thread_mutation_errors(req.execution_id, req.thread_id):
            staged = await self._runtime.threads.replace_action(
                req.execution_id, req.thread_id, req.target_command, action_template,
                reason=reason, confirm=True,
            )
        return ReplaceResult(
            execution_id=req.execution_id, thread_id=req.thread_id,
            staged_for_recovery=staged,
            recommended_decision=RecoveryDecision.ABORT_ACTION if staged else None,
            next_step=_STAGED_ACTION_NEXT if staged else None,
        )


THREAD_OPERATIONS = (
    PauseOperation,
    ResumeOperation,
    SpawnThreadOperation,
    RecoverThreadOperation,
    SkipMethodOperation,
    SkipActionOperation,
    InsertMethodOperation,
    ReplaceMethodOperation,
    InsertActionOperation,
    ReplaceActionOperation,
)
