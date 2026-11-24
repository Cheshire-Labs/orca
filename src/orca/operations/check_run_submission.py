"""A check-run submission runs in the check runs' own runtime; any other runs on the API's runtime."""

from collections.abc import Awaitable, Callable
from typing import Generic, Protocol, TypeVar

from orca.operations.submission import (
    SubmitExecutionOperation,
    SubmitMethodExecutionOperation,
    other_run_mode_active,
)
from orca.operations.submission_models import (
    MethodExecutionRequest,
    MethodExecutionResponse,
    SubmitExecutionRequest,
    SubmitExecutionResponse,
)
from orca.runtime.check_runs import CheckRuns
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.errors import OtherRunModeActiveError

RequestT = TypeVar("RequestT", SubmitExecutionRequest, MethodExecutionRequest)
ResponseT = TypeVar("ResponseT", SubmitExecutionResponse, MethodExecutionResponse)
_RequestContra = TypeVar("_RequestContra", contravariant=True)
_ResponseCo = TypeVar("_ResponseCo", covariant=True)


class ICheckRunContext(Protocol):
    def runtime(self) -> ISystemRuntime: ...

    def check_runs(self) -> CheckRuns | None:
        """None until a system is built."""
        ...


class _Submits(Protocol[_RequestContra, _ResponseCo]):
    def run(self, req: _RequestContra) -> Awaitable[_ResponseCo]: ...


class RoutedSubmission(Generic[RequestT, ResponseT]):
    def __init__(
        self, context: ICheckRunContext, submit_on: Callable[[ISystemRuntime], _Submits[RequestT, ResponseT]],
    ) -> None:
        self._context = context
        self._submit_on: Callable[[ISystemRuntime], _Submits[RequestT, ResponseT]] = submit_on

    async def run(self, req: RequestT) -> ResponseT:
        check_runs = self._context.check_runs()
        system = self._context.runtime()
        if check_runs is None:
            return await self._submit_on(system).run(req)
        try:
            if req.run_mode != "CHECK_RUN":
                async with check_runs.not_running(WorkflowRunMode[req.run_mode]):
                    return await self._submit_on(system).run(req)
            async with check_runs.submitting() as runtime:
                response = await self._submit_on(runtime).run(req)
                check_runs.accepted(response.execution_id)
        except OtherRunModeActiveError as exc:
            raise other_run_mode_active(exc) from exc
        return response


def routed_submit_execution(context: ICheckRunContext) -> RoutedSubmission[SubmitExecutionRequest, SubmitExecutionResponse]:
    return RoutedSubmission(context, lambda runtime: SubmitExecutionOperation(runtime))


def routed_submit_method(context: ICheckRunContext) -> RoutedSubmission[MethodExecutionRequest, MethodExecutionResponse]:
    return RoutedSubmission(context, lambda runtime: SubmitMethodExecutionOperation(runtime))
