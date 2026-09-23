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

from typing import ClassVar, Literal
from pydantic import BaseModel
from orca.operations._protocol import OperationError, message_of
from orca.operations._scope import ExecutionScope, Scope
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
    AbortMethodRequest,
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
    _ResumeExecutionResult,
    _ResumeThreadResult,
    _ThreadMutationResult,
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
    Request: ClassVar[type[BaseModel]] = PauseRequest
    Response: ClassVar[type[BaseModel]] = PauseResponse

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
            raise OperationError.not_found(_describe_not_found(scope)) from exc


# -- Resume Operation --------------------------------------------------------


class ResumeOperation:
    Request: ClassVar[type[BaseModel]] = ResumeRequest
    Response: ClassVar[type[BaseModel]] = ResumeResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: ResumeRequest) -> ResumeResponse:
        scope = req.scope
        try:
            if isinstance(scope, ExecutionScope):
                result = await self._runtime.threads.resume_all(
                    scope.execution_id, reason=req.reason, confirm=True,
                )
                return ResumeResponse(result=_ResumeExecutionResult(
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
            raise OperationError.not_found(_describe_not_found(scope)) from exc
        except ValueError as exc:
            raise OperationError.conflict(str(exc)) from exc


# -- SpawnThread Operation ---------------------------------------------------


class SpawnThreadOperation:
    Request: ClassVar[type[BaseModel]] = SpawnThreadRequest
    Response: ClassVar[type[BaseModel]] = SpawnThreadResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: SpawnThreadRequest) -> SpawnThreadResponse:
        try:
            snap = await self._runtime.threads.spawn_thread(
                req.execution_id, req.template_name,
                labware_id=req.labware_id, confirm=True,
            )
        except KeyError as exc:
            missing = exc.args[0] if exc.args else (
                f"{req.execution_id}/{req.template_name}"
            )
            raise OperationError.not_found(
                f"execution or template {missing!s} not found",
                execution_id=req.execution_id,
            ) from exc
        except (RuntimeError, ValueError) as exc:
            raise OperationError.conflict(str(exc)) from exc
        return SpawnThreadResponse.from_snapshot(snap)


# -- RecoverThread Operation -------------------------------------------------


class RecoverThreadOperation:
    Request: ClassVar[type[BaseModel]] = RecoverThreadRequest
    Response: ClassVar[type[BaseModel]] = RecoverThreadResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: RecoverThreadRequest) -> RecoverThreadResponse:
        try:
            decision = RecoveryDecision[req.decision.upper()]
        except KeyError as exc:
            valid = ", ".join(d.name for d in RecoveryDecision)
            raise OperationError.invalid_input(
                f"decision {req.decision!r} unknown; must be one of {valid}",
            ) from exc

        try:
            await self._runtime.threads.recover(
                req.execution_id, req.thread_id, decision,
                confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"thread {req.execution_id}/{req.thread_id} not found",
                execution_id=req.execution_id,
                thread_id=req.thread_id,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        except RuntimeError as exc:
            raise OperationError.conflict(str(exc)) from exc
        return RecoverThreadResponse(
            execution_id=req.execution_id,
            thread_id=req.thread_id,
        )


def _describe_not_found(scope: Scope) -> str:
    if isinstance(scope, ExecutionScope):
        return f"execution {scope.execution_id} not found"
    return f"thread {scope.execution_id}/{scope.thread_id} not found"


# -- Skip + abort -----------------------------------------------------------
#
# Select-by-id-or-name plus an audit reason. InsertMethod and InsertAction
# need more than that, so they have their own section below.


def _validate_one_selector(
    *, kind: Literal["method", "action"],
    by_id: str | None, by_name: str | None,
) -> None:
    """Reject if neither or both selectors were given.

    The facade methods enforce this internally with `ValueError`; the
    Operation surfaces it as `invalid_input` upfront so the wire
    contract is documented in one place.
    """
    given = (by_id is not None) + (by_name is not None)
    if given == 0:
        raise OperationError.invalid_input(
            f"provide exactly one of {kind}_id or {kind}_name",
        )
    if given == 2:
        raise OperationError.invalid_input(
            f"provide ONLY one of {kind}_id or {kind}_name, not both",
        )


def _require_reason_text(reason: str | None) -> str:
    if reason is None or not reason.strip():
        raise OperationError.invalid_input("reason is required and cannot be empty")
    return reason


# -- SkipMethodOperation ----------------------------------------------------


class SkipMethodOperation:
    Request: ClassVar[type[BaseModel]] = SkipMethodRequest
    Response: ClassVar[type[BaseModel]] = _ThreadMutationResult

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: SkipMethodRequest) -> _ThreadMutationResult:
        _validate_one_selector(
            kind="method", by_id=req.method_id, by_name=req.method_name,
        )
        reason = _require_reason_text(req.reason)
        try:
            await self._runtime.threads.skip_method(
                req.execution_id, req.thread_id,
                method_id=req.method_id, method_name=req.method_name,
                reason=reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                message_of(exc), execution_id=req.execution_id, thread_id=req.thread_id,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        except RuntimeError as exc:
            raise OperationError.conflict(str(exc)) from exc
        return _ThreadMutationResult(
            execution_id=req.execution_id, thread_id=req.thread_id,
        )


# -- AbortMethodOperation ---------------------------------------------------


class AbortMethodOperation:
    Request: ClassVar[type[BaseModel]] = AbortMethodRequest
    Response: ClassVar[type[BaseModel]] = _ThreadMutationResult

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: AbortMethodRequest) -> _ThreadMutationResult:
        _validate_one_selector(
            kind="method", by_id=req.method_id, by_name=req.method_name,
        )
        reason = _require_reason_text(req.reason)
        try:
            await self._runtime.threads.abort_method(
                req.execution_id, req.thread_id,
                method_id=req.method_id, method_name=req.method_name,
                reason=reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                message_of(exc), execution_id=req.execution_id, thread_id=req.thread_id,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        except RuntimeError as exc:
            raise OperationError.conflict(str(exc)) from exc
        return _ThreadMutationResult(
            execution_id=req.execution_id, thread_id=req.thread_id,
        )


# -- SkipActionOperation ----------------------------------------------------


class SkipActionOperation:
    Request: ClassVar[type[BaseModel]] = SkipActionRequest
    Response: ClassVar[type[BaseModel]] = _ThreadMutationResult

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: SkipActionRequest) -> _ThreadMutationResult:
        _validate_one_selector(
            kind="action", by_id=req.action_id, by_name=req.action_command,
        )
        reason = _require_reason_text(req.reason)
        try:
            await self._runtime.threads.skip_action(
                req.execution_id, req.thread_id,
                action_id=req.action_id, action_command=req.action_command,
                reason=reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                message_of(exc), execution_id=req.execution_id, thread_id=req.thread_id,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        except RuntimeError as exc:
            raise OperationError.conflict(str(exc)) from exc
        return _ThreadMutationResult(
            execution_id=req.execution_id, thread_id=req.thread_id,
        )


# -- Insert method + insert action ------------------------------------------
#
# These ops compile or look up a template, validate the insert position,
# and call `threads.insert_method` / `insert_action`. The hosted layer's
# thread-mutation handler also runs a best-effort
# labware-compat static check before inserting; that check stays on the
# legacy handler (out of orca-core's source-available boundary -- it lives in the
# hosted layer's `_validate_insert_method_labware_compat` helper). A later change can fold
# the labware-compat check into a hosted-side wrapper around the Operation.


def _resolve_insert_position(
    where: InsertWhere, anchor: str | None,
) -> InsertPosition:
    """Map (where, anchor) -> concrete InsertPosition value.

    head/tail forbid anchor; before/after require it. Mirrors the
    legacy daemon + hosted handler helpers of the same name so the
    wire contract is preserved. ``where`` is constrained at the
    Pydantic request layer (``Literal["head", "tail", "before",
    "after"]``) so the runtime arm of the match below is a closed
    set; the unreachable ``else`` is kept defensive but typed
    callers never reach it.
    """
    if where == "head":
        if anchor is not None:
            raise OperationError.invalid_input("anchor must be omitted for where='head'")
        return AtHead()
    if where == "tail":
        if anchor is not None:
            raise OperationError.invalid_input("anchor must be omitted for where='tail'")
        return AtTail()
    if where == "before":
        if not anchor:
            raise OperationError.invalid_input("anchor is required for where='before'")
        return Before(anchor_name=anchor)
    if where == "after":
        if not anchor:
            raise OperationError.invalid_input("anchor is required for where='after'")
        return After(anchor_name=anchor)
    raise OperationError.invalid_input(
        f"unknown where {where!r}; must be one of head/tail/before/after",
    )


class InsertMethodOperation:
    Request: ClassVar[type[BaseModel]] = InsertMethodRequest
    Response: ClassVar[type[BaseModel]] = _ThreadMutationResult

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: InsertMethodRequest) -> _ThreadMutationResult:
        # Eager-validate cheap shape errors before any template/code work.
        reason = _require_reason_text(req.reason)
        if (req.template_name is None) == (req.method_code is None):
            raise OperationError.invalid_input(
                "provide exactly one of template_name or method_code",
            )
        where = _resolve_insert_position(req.where, req.anchor)

        if req.template_name is not None:
            try:
                execution = self._runtime.get_execution(req.execution_id)
            except KeyError as exc:
                raise OperationError.not_found(
                    f"no execution {req.execution_id!r}",
                    execution_id=req.execution_id,
                ) from exc
            try:
                template = self._runtime.registry.get_method_template(
                    execution.workflow_name, req.template_name,
                )
            except KeyError as exc:
                raise OperationError.not_found(
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
                raise OperationError.invalid_input(str(exc)) from exc

        try:
            await self._runtime.threads.insert_method(
                req.execution_id, req.thread_id, template, where,
                reason=reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                message_of(exc),
                execution_id=req.execution_id, thread_id=req.thread_id,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        except RuntimeError as exc:
            raise OperationError.conflict(str(exc)) from exc
        return _ThreadMutationResult(
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
    Request: ClassVar[type[BaseModel]] = ReplaceMethodRequest
    Response: ClassVar[type[BaseModel]] = ReplaceResult

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: ReplaceMethodRequest) -> ReplaceResult:
        reason = _require_reason_text(req.reason)
        if (req.template_name is None) == (req.method_code is None):
            raise OperationError.invalid_input(
                "provide exactly one of template_name or method_code",
            )

        if req.template_name is not None:
            try:
                execution = self._runtime.get_execution(req.execution_id)
            except KeyError as exc:
                raise OperationError.not_found(
                    f"no execution {req.execution_id!r}",
                    execution_id=req.execution_id,
                ) from exc
            try:
                template = self._runtime.registry.get_method_template(
                    execution.workflow_name, req.template_name,
                )
            except KeyError as exc:
                raise OperationError.not_found(
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
                raise OperationError.invalid_input(str(exc)) from exc

        try:
            staged = await self._runtime.threads.replace_method(
                req.execution_id, req.thread_id, req.target_name, template,
                reason=reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                message_of(exc),
                execution_id=req.execution_id, thread_id=req.thread_id,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        except RuntimeError as exc:
            raise OperationError.conflict(str(exc)) from exc
        return ReplaceResult(
            execution_id=req.execution_id, thread_id=req.thread_id,
            staged_for_recovery=staged,
            recommended_decision=RecoveryDecision.ABORT_METHOD if staged else None,
            next_step=_STAGED_METHOD_NEXT if staged else None,
        )


class InsertActionOperation:
    Request: ClassVar[type[BaseModel]] = InsertActionRequest
    Response: ClassVar[type[BaseModel]] = _ThreadMutationResult

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: InsertActionRequest) -> _ThreadMutationResult:
        reason = _require_reason_text(req.reason)
        where = _resolve_insert_position(req.where, req.anchor)

        try:
            action_template = compile_action_code(
                req.action_code, LiveTopology(self._runtime.system),
            )
        except (CodeValidationError, CodeInjectionError) as exc:
            raise OperationError.invalid_input(str(exc)) from exc

        try:
            await self._runtime.threads.insert_action(
                req.execution_id, req.thread_id, action_template, where,
                reason=reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                message_of(exc),
                execution_id=req.execution_id, thread_id=req.thread_id,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        except RuntimeError as exc:
            raise OperationError.conflict(str(exc)) from exc
        return _ThreadMutationResult(
            execution_id=req.execution_id, thread_id=req.thread_id,
        )


class ReplaceActionOperation:
    Request: ClassVar[type[BaseModel]] = ReplaceActionRequest
    Response: ClassVar[type[BaseModel]] = ReplaceResult

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: ReplaceActionRequest) -> ReplaceResult:
        reason = _require_reason_text(req.reason)
        try:
            action_template = compile_action_code(
                req.action_code, LiveTopology(self._runtime.system),
            )
        except (CodeValidationError, CodeInjectionError) as exc:
            raise OperationError.invalid_input(str(exc)) from exc

        try:
            staged = await self._runtime.threads.replace_action(
                req.execution_id, req.thread_id, req.target_command, action_template,
                reason=reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                message_of(exc),
                execution_id=req.execution_id, thread_id=req.thread_id,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        except RuntimeError as exc:
            raise OperationError.conflict(str(exc)) from exc
        return ReplaceResult(
            execution_id=req.execution_id, thread_id=req.thread_id,
            staged_for_recovery=staged,
            recommended_decision=RecoveryDecision.ABORT_ACTION if staged else None,
            next_step=_STAGED_ACTION_NEXT if staged else None,
        )
