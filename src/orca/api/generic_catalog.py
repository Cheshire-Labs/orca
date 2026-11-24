"""The Operations every Orca server serves."""

from collections.abc import Callable, Sequence
from typing import Protocol

from pydantic import BaseModel

from orca.api.interfaces import IApiContext, OperationEntry
from orca.operations.access_configs import ACCESS_CONFIG_OPERATIONS
from orca.operations.audit import ListAuditOperation
from orca.operations.binding import BoundOperation, RunnableOperation
from orca.operations.catalog import CATALOG_OPERATIONS
from orca.operations.deck_layouts import DECK_LAYOUT_OPERATIONS, UpdateDeckLayoutOperation
from orca.operations.device import DEVICE_LIFECYCLE_OPERATIONS, DEVICE_OPERATIONS
from orca.operations.device_commands import DEVICE_COMMAND_READ_OPERATIONS
from orca.operations.device_registry import DEVICE_REGISTRY_OPERATIONS
from orca.operations.device_setups import GetDeviceSetupOperation, PutDeviceSetupOperation
from orca.operations.events import ListEventsOperation
from orca.operations.grip_profiles import GRIP_PROFILE_OPERATIONS
from orca.operations.execution import (
    EXECUTION_CONTROL_OPERATIONS,
    CheckRunExecutions,
    ExecutionReader,
    GetExecutionDetailOperation,
    ListExecutionsOperation,
)
from orca.operations.incidents import INCIDENT_OPERATIONS
from orca.operations.interventions import NextInterventionOperation
from orca.operations.labware import (
    LABWARE_OPERATIONS,
    ClearAllLabwareOperation,
    DischargeLabwareOperation,
    RegisterLabwareOperation,
    SetWellVolumesOperation,
)
from orca.operations.run_loading import LoadRunLabwareOperation, LoadSteps
from orca.operations.labware_catalog import LABWARE_CATALOG_OPERATIONS
from orca.operations.manual_step import MANUAL_STEP_OPERATIONS
from orca.operations.move_defaults import MOVE_DEFAULTS_OPERATIONS, ListMoveDefaultsOperation
from orca.operations.ops_history import OPS_HISTORY_OPERATIONS
from orca.operations.operator_device import (
    COMMANDS_BY_NAME,
    DEVICE_COMMAND_RUNNER_OPERATIONS,
    DeviceBatchExecuteOperation,
    LiquidHandlerConfigureDeckOperation,
    SaveCurrentPositionOperation,
    TakesTheRunner,
    TakesTheRuntimeAndTheRunner,
    TransporterMoveToOperation,
    TransporterOpenGripperOperation,
)
from orca.operations.profiles import PROFILE_OPERATIONS
from orca.operations.runtime_reads import RUNTIME_READ_OPERATIONS, GetThreadOperation
from orca.operations.runtime_status import GetRuntimeStatusOperation
from orca.operations.system_loading import ReloadRuntimeOperation
from orca.operations.state import UnsettledStateOperation
from orca.operations.check_run_submission import routed_submit_execution, routed_submit_method
from orca.operations.check_runs import GetCheckRunLabwareOperation
from orca.operations.submission_models import (
    GetSubmissionRequest,
    GetSubmissionResponse,
    ListSubmissionsRequest,
    ListSubmissionsResponse,
)
from orca.operations.submission import (
    GetSubmissionOperation,
    ListSubmissionsOperation,
    SubmitExecutionOperation,
    SubmitMethodExecutionOperation,
)
from orca.operations.system import GetSystemInfoOperation
from orca.operations.teachpoints import (
    TEACHPOINT_RUNTIME_OPERATIONS,
    DeleteTeachpointOperation,
    UpdateTeachpointOperation,
)
from orca.operations.teachpoint_transfer import CreateTeachpointsOperation
from orca.operations.thread import THREAD_OPERATIONS
from orca.operations.typed_device_commands import TYPED_COMMAND_OPERATIONS
from orca.operations.topology import GetTopologyOperation
from orca.operations.variables import VARIABLE_OPERATIONS
from orca.runtime.deployment_registries import IDeploymentRegistries
from orca.runtime.runtime_interface import ISystemRuntime

_NEEDS_ONLY_THE_RUNTIME = (
    *VARIABLE_OPERATIONS,
    *INCIDENT_OPERATIONS,
    *RUNTIME_READ_OPERATIONS,
    *DEVICE_REGISTRY_OPERATIONS,
    *THREAD_OPERATIONS,
    *MANUAL_STEP_OPERATIONS,
    *EXECUTION_CONTROL_OPERATIONS,
    *LABWARE_OPERATIONS,
    *DEVICE_OPERATIONS,
)

_NEEDS_THE_RUNTIME_AND_THE_RUNNER = DEVICE_LIFECYCLE_OPERATIONS

_NEEDS_ONLY_THE_RUNTIME_AFTER = (
    *DEVICE_COMMAND_READ_OPERATIONS,
    *CATALOG_OPERATIONS,
    *OPS_HISTORY_OPERATIONS,
    *DECK_LAYOUT_OPERATIONS,
    *TEACHPOINT_RUNTIME_OPERATIONS,
    GetSystemInfoOperation,
    UnsettledStateOperation,
)

_NEEDS_ONLY_THE_REGISTRIES = (
    *ACCESS_CONFIG_OPERATIONS,
    *GRIP_PROFILE_OPERATIONS,
    *LABWARE_CATALOG_OPERATIONS,
    *MOVE_DEFAULTS_OPERATIONS,
    *PROFILE_OPERATIONS,
    GetDeviceSetupOperation,
)


class _TakesTheRuntime(BoundOperation, Protocol):
    def __init__(self, *, runtime: ISystemRuntime) -> None: ...


def _named_by(req: BaseModel, field: str) -> str | None:
    """The value a request names for `field`, at its top level or in its scope."""
    fields = req.model_dump(mode="json")
    scope = fields.get("scope")
    for value in (fields.get(field), scope.get(field) if isinstance(scope, dict) else None):
        if isinstance(value, str):
            return value
    return None


def check_run_holding(context: IApiContext, req: BaseModel) -> ISystemRuntime | None:
    """The running check run's runtime when the request names one of its executions or submissions."""
    check_runs = context.check_runs()
    if check_runs is None:
        return None
    execution_id = _named_by(req, "execution_id")
    if execution_id is not None:
        return check_runs.runtime_for(execution_id)
    submission_id = _named_by(req, "submission_id")
    if submission_id is not None:
        return check_runs.runtime_for_submission(submission_id)
    return None


class _OnTheRuntimeThatHoldsIt:
    """Builds the operation on the running check run's runtime when the request names one of its executions.

    Every other request acts on the system, so an operator's work during a check run lands on the real record.
    """

    def __init__(self, context: IApiContext, build: Callable[[ISystemRuntime], RunnableOperation]) -> None:
        self._context = context
        self._build = build

    async def run(self, req: BaseModel) -> BaseModel:
        runtime = check_run_holding(self._context, req) or self._context.runtime()
        result: BaseModel = await self._build(runtime).run(req)
        return result


class _SubmissionsOnBothRuntimes:
    """Every submission, every kept check run's included, or the named execution's from whichever holds it."""

    def __init__(self, context: IApiContext) -> None:
        self._context = context

    async def run(self, req: ListSubmissionsRequest) -> ListSubmissionsResponse:
        system = self._context.runtime()
        check_runs = self._context.check_runs()
        if check_runs is None:
            return await ListSubmissionsOperation(runtime=system).run(req)
        if req.execution_id is not None:
            held = check_runs.submissions.holds_execution(req.execution_id)
            return await ListSubmissionsOperation(runtime=check_runs if held else system).run(req)
        listed = await ListSubmissionsOperation(runtime=system).run(req)
        also = await ListSubmissionsOperation(runtime=check_runs).run(req)
        return ListSubmissionsResponse(submissions=[*listed.submissions, *also.submissions])


class _SubmissionOnEitherRuntime:
    """A submission from the system, or from a kept check run, which holds it after its runtime is gone."""

    def __init__(self, context: IApiContext) -> None:
        self._context = context

    async def run(self, req: GetSubmissionRequest) -> GetSubmissionResponse:
        system = self._context.runtime()
        check_runs = self._context.check_runs()
        if check_runs is not None and check_runs.submissions.holds(req.submission_id):
            return await GetSubmissionOperation(runtime=check_runs).run(req)
        return await GetSubmissionOperation(runtime=system).run(req)


def on_the_runtime_that_holds_it(
    op_class: type[BoundOperation], build: Callable[[IApiContext, ISystemRuntime], RunnableOperation],
) -> OperationEntry[IApiContext]:
    return OperationEntry(op_class, lambda context: _OnTheRuntimeThatHoldsIt(context, lambda runtime: build(context, runtime)))


def _can_name_an_execution(op_class: type[BoundOperation]) -> bool:
    return not {"execution_id", "submission_id", "scope"}.isdisjoint(op_class.Request.model_fields)


def with_runtime(op_class: type[_TakesTheRuntime]) -> OperationEntry[IApiContext]:
    """The entry for an Operation whose only dependency is the runtime."""
    if _can_name_an_execution(op_class):
        return on_the_runtime_that_holds_it(op_class, lambda _context, runtime: op_class(runtime=runtime))
    return OperationEntry(op_class, lambda context: op_class(runtime=context.runtime()))


async def _no_check_runs() -> set[str]:
    return set()


def _check_run_executions(context: IApiContext) -> CheckRunExecutions:
    check_runs = context.check_runs()
    return check_runs.check_run_executions if check_runs is not None else _no_check_runs


class _TakesTheRegistries(BoundOperation, Protocol):
    def __init__(self, *, registries: IDeploymentRegistries) -> None: ...


def _with_registries(op_class: type[_TakesTheRegistries]) -> OperationEntry[IApiContext]:
    return OperationEntry(
        op_class, lambda context: op_class(registries=context.deployment_registries()),
    )


def _list_move_defaults_op(context: IApiContext) -> ListMoveDefaultsOperation:
    runtime = context.built_runtime()
    return ListMoveDefaultsOperation(
        registries=context.deployment_registries(),
        system=runtime.system if runtime is not None else None,
    )


def _put_device_setup_op(context: IApiContext) -> PutDeviceSetupOperation:
    runtime = context.built_runtime()
    return PutDeviceSetupOperation(
        registries=context.deployment_registries(),
        system=runtime.system if runtime is not None else None,
    )


def _update_teachpoint_op(context: IApiContext) -> UpdateTeachpointOperation:
    return UpdateTeachpointOperation(
        runtime=context.runtime(), access_configs=context.deployment_registries().access_configs,
    )


def _create_teachpoints_op(context: IApiContext) -> CreateTeachpointsOperation:
    return CreateTeachpointsOperation(
        runtime=context.runtime(), registries=context.deployment_registries(),
    )


def _delete_teachpoint_op(context: IApiContext) -> DeleteTeachpointOperation:
    return DeleteTeachpointOperation(
        runtime=context.built_runtime(), setup_stores=context.setup_stores(),
    )


def _list_executions_op(context: IApiContext, runtime: ISystemRuntime) -> ListExecutionsOperation:
    return ListExecutionsOperation(
        runtime=runtime, terminal_list=runtime.execution_records.list_records,
        check_run_executions=_check_run_executions(context),
    )


def _execution_reader(context: IApiContext, runtime: ISystemRuntime) -> ExecutionReader:
    return ExecutionReader(
        runtime=runtime, terminal_detail_lookup=runtime.execution_records.get_detail,
        check_run_executions=_check_run_executions(context),
    )


def _load_run_labware_op(context: IApiContext) -> LoadRunLabwareOperation:
    runtime = context.runtime()
    return LoadRunLabwareOperation(
        runtime=runtime,
        catalog=context.deployment_registries().labware_catalog,
        steps=LoadSteps(
            discharge=DischargeLabwareOperation(runtime),
            register=RegisterLabwareOperation(runtime),
            set_well_volumes=SetWellVolumesOperation(runtime),
            clear_all=ClearAllLabwareOperation(runtime),
        ),
    )


def _with_the_runner(op_class: type[TakesTheRunner]) -> OperationEntry[IApiContext]:
    return OperationEntry(op_class, lambda context: op_class(devices=context.device_commands()))


def _with_the_runtime_and_the_runner(op_class: type[TakesTheRuntimeAndTheRunner]) -> OperationEntry[IApiContext]:
    return OperationEntry(op_class, lambda context: op_class(runtime=context.runtime(), devices=context.device_commands()))


def _by_name(op_class: type[DeviceBatchExecuteOperation]) -> OperationEntry[IApiContext]:
    return OperationEntry(
        op_class, lambda context: op_class(runtime=context.built_runtime(), devices=context.device_commands()),
    )


def _operator_device_entries() -> tuple[OperationEntry[IApiContext], ...]:
    """The operator device commands."""
    return (
        *(_by_name(op) for op in COMMANDS_BY_NAME),
        *(_with_the_runner(op) for op in DEVICE_COMMAND_RUNNER_OPERATIONS),
        OperationEntry(
            TransporterOpenGripperOperation,
            lambda context: TransporterOpenGripperOperation(
                devices=context.device_commands(), runtime=context.built_runtime(),
            ),
        ),
        _with_the_runtime_and_the_runner(TransporterMoveToOperation),
        OperationEntry(
            LiquidHandlerConfigureDeckOperation,
            lambda context: LiquidHandlerConfigureDeckOperation(
                devices=context.device_commands(), runtime=context.runtime(),
            ),
        ),
        OperationEntry(
            SaveCurrentPositionOperation,
            lambda context: SaveCurrentPositionOperation(
                devices=context.device_commands(), runtime=context.built_runtime(),
                registries=context.deployment_registries(),
            ),
        ),
    )


class GenericOperationCatalog:
    """The Operations served the same way on every server."""

    def entries(self) -> Sequence[OperationEntry[IApiContext]]:
        return (
            on_the_runtime_that_holds_it(
                GetThreadOperation,
                lambda context, runtime: GetThreadOperation(runtime, _execution_reader(context, runtime)),
            ),
            *(with_runtime(op) for op in _NEEDS_ONLY_THE_RUNTIME),
            *(_with_the_runtime_and_the_runner(op) for op in _NEEDS_THE_RUNTIME_AND_THE_RUNNER),
            *(with_runtime(op) for op in _NEEDS_ONLY_THE_RUNTIME_AFTER),
            *(_with_registries(op) for op in _NEEDS_ONLY_THE_REGISTRIES),
            OperationEntry(GetTopologyOperation, lambda context: GetTopologyOperation(reader=context)),
            OperationEntry(GetRuntimeStatusOperation, lambda context: GetRuntimeStatusOperation(state=context)),
            OperationEntry(
                ReloadRuntimeOperation,
                lambda context: ReloadRuntimeOperation(
                    holder=context.runtime_holder(), revision=context.package_revision,
                ),
            ),
            OperationEntry(ListAuditOperation, lambda context: ListAuditOperation(audit_log=context.audit_log())),
            OperationEntry(ListEventsOperation, lambda context: ListEventsOperation(event_log=context.event_log())),
            OperationEntry(
                NextInterventionOperation, lambda context: NextInterventionOperation(event_log=context.event_log()),
            ),
            OperationEntry(ListMoveDefaultsOperation, _list_move_defaults_op),
            OperationEntry(PutDeviceSetupOperation, _put_device_setup_op),
            OperationEntry(
                UpdateDeckLayoutOperation,
                lambda context: UpdateDeckLayoutOperation(
                    runtime=context.runtime(), registries=context.deployment_registries(),
                ),
            ),
            OperationEntry(UpdateTeachpointOperation, _update_teachpoint_op),
            OperationEntry(DeleteTeachpointOperation, _delete_teachpoint_op),
            OperationEntry(CreateTeachpointsOperation, _create_teachpoints_op),
            OperationEntry(SubmitExecutionOperation, routed_submit_execution),
            OperationEntry(ListSubmissionsOperation, _SubmissionsOnBothRuntimes),
            OperationEntry(GetSubmissionOperation, _SubmissionOnEitherRuntime),
            OperationEntry(
                GetCheckRunLabwareOperation,
                lambda context: GetCheckRunLabwareOperation(check_runs=context.check_runs()),
            ),
            OperationEntry(SubmitMethodExecutionOperation, routed_submit_method),
            OperationEntry(LoadRunLabwareOperation, _load_run_labware_op),
            on_the_runtime_that_holds_it(ListExecutionsOperation, _list_executions_op),
            on_the_runtime_that_holds_it(
                GetExecutionDetailOperation,
                lambda context, runtime: GetExecutionDetailOperation(_execution_reader(context, runtime)),
            ),
            *_operator_device_entries(),
            *(_with_the_runner(op) for op in TYPED_COMMAND_OPERATIONS),
        )
