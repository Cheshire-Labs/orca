"""Router that mounts Operation-bound endpoints alongside the legacy routes.

`/operations/*` carries the Operation bindings; the legacy CLI-facing routes
(`/system`, `/health`, `/mount-topology`, `/executions/*`) keep working beside
them. The Operations the CLI needs are bound here, so it reaches them from a
local daemon with no hosted deployment in the path.
"""

import json
from typing import TypeVar

from fastapi import APIRouter, Request
from pydantic import BaseModel, ValidationError

from orca.daemon.bindings import bind_orca_rest
from orca.daemon.route_tags import RouteTag
from orca.operations._protocol import OperationError
from orca.operations.catalog import (
    GetMethodOperation,
    GetMethodRequest,
    GetMethodResponse,
    GetWorkflowOperation,
    GetWorkflowRequest,
    GetWorkflowResponse,
    ListLocationsOperation,
    ListLocationsResponse,
    ListMethodsOperation,
    ListMethodsResponse,
    ListThreadTemplatesOperation,
    ListThreadTemplatesResponse,
    ListWorkflowsOperation,
    ListWorkflowsResponse,
    _EmptyRequest as _CatalogEmptyRequest,
)
from orca.operations.state import UnsettledStateOperation
from orca.operations.state_models import UnsettledStateRequest, UnsettledStateResponse
from orca.operations.device import (
    ConnectDeviceOperation,
    DisconnectDeviceOperation,
    GetDeviceStatusOperation,
    InitializeDeviceOperation,
    CompareDeckOperation,
    ConfirmMountedTipsOperation,
    GetMountedTipsOperation,
    SetMountedTipsOperation,
    ClearDeviceFaultOperation,
    ReleaseDeviceControlOperation,
    TakeDeviceControlOperation,
    ReconcileDeckOperation,
    ListDevicesOperation,
)
from orca.operations.device_models import (
    ConnectDeviceRequest,
    ConnectDeviceResponse,
    DisconnectDeviceRequest,
    DisconnectDeviceResponse,
    GetDeviceStatusRequest,
    GetDeviceStatusResponse,
    InitializeDeviceRequest,
    InitializeDeviceResponse,
    ConfirmMountedTipsRequest,
    GetMountedTipsRequest,
    GetMountedTipsResponse,
    MountedTipsMutationResponse,
    SetMountedTipsRequest,
    CompareDeckRequest,
    CompareDeckResponse,
    ClearDeviceFaultRequest,
    ClearDeviceFaultResponse,
    ReleaseDeviceControlRequest,
    ReleaseDeviceControlResponse,
    TakeDeviceControlRequest,
    TakeDeviceControlResponse,
    ReconcileDeckRequest,
    ReconcileDeckResponse,
    ListDevicesRequest,
    ListDevicesResponse,
)
from orca.operations.execution import (
    CloseExecutionOperation,
    GetExecutionDetailOperation,
    GetExecutionOperation,
    ListExecutionsOperation,
    RemoveExecutionOperation,
    StopExecutionOperation,
)
from orca.operations.execution_models import (
    CloseExecutionRequest,
    CloseExecutionResponse,
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
from orca.operations.labware import (
    ReleaseMoverHoldOperation,
    ClearLabwareCarryOverrideOperation,
    EditLabwareBarcodeOperation,
    SetLabwareCarryOverrideOperation,
    EditLabwareLocationOperation,
    GetLabwareByBarcodeOperation,
    GetLabwareByIdOperation,
    GetLabwareHistoryOperation,
    GetLabwareJourneyOperation,
    GetTipStateOperation,
    MarkTipsUsedOperation,
    ResolveContentsOperation,
    GetWellVolumesOperation,
    ConfirmTipStateOperation,
    ConfirmWellVolumesOperation,
    SetTipStateOperation,
    ListLabwareOperation,
    RegisterLabwareOperation,
    ResetLabwareLocationOperation,
    SetWellVolumesOperation,
)
from orca.operations.labware_models import (
    ReleaseMoverHoldRequest,
    ReleaseMoverHoldResponse,
    ClearLabwareCarryOverrideRequest,
    CarryOverrideResponse,
    SetLabwareCarryOverrideRequest,
    EditLabwareBarcodeRequest,
    EditLabwareLocationRequest,
    GetLabwareByBarcodeRequest,
    GetLabwareByIdRequest,
    GetLabwareHistoryRequest,
    GetLabwareHistoryResponse,
    GetLabwareJourneyRequest,
    GetLabwareJourneyResponse,
    GetLabwareResponse,
    MarkTipsUsedRequest,
    MarkTipsUsedResponse,
    ResolveContentsRequest,
    ResolveContentsResponse,
    GetTipStateRequest,
    GetTipStateResponse,
    GetWellVolumesRequest,
    GetWellVolumesResponse,
    ConfirmTipStateRequest,
    ConfirmWellVolumesRequest,
    SetTipStateRequest,
    ListLabwareRequest,
    ListLabwareResponse,
    RegisterLabwareRequest,
    RegisterLabwareResponse,
    ResetLabwareLocationRequest,
    SetWellVolumesRequest,
    _MutationResponse,
)
from orca.operations.manual_step import (
    ConfirmManualStepOperation,
    ListPendingManualStepsOperation,
)
from orca.operations.manual_step_models import (
    ConfirmManualStepRequest,
    ConfirmManualStepResponse,
    ListPendingManualStepsRequest,
    ListPendingManualStepsResponse,
)
from orca.operations.ops_history import (
    ListOpsHistoryOperation,
    ListOpsHistoryRequest,
    ListOpsHistoryResponse,
    SearchOpsHistoryOperation,
    SearchOpsHistoryRequest,
    SearchOpsHistoryResponse,
)
from orca.operations.system import (
    GetSystemInfoOperation,
    GetSystemInfoRequest,
    GetSystemInfoResponse,
)
from orca.operations.thread import (
    AbortMethodOperation,
    InsertActionOperation,
    InsertMethodOperation,
    PauseOperation,
    RecoverThreadOperation,
    ReplaceActionOperation,
    ReplaceMethodOperation,
    ResumeOperation,
    SkipActionOperation,
    SkipMethodOperation,
    SpawnThreadOperation,
)
from orca.operations.thread_models import (
    AbortMethodRequest,
    InsertActionRequest,
    InsertMethodRequest,
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
    _ThreadMutationResult,
)
from orca.runtime.system_runtime import SystemRuntime


_R = TypeVar("_R", bound=BaseModel)


def create_operations_router() -> APIRouter:
    """Build the Operation-bound router.

    Daemon factory mounts this with no prefix; each binding declares its
    own path under `/operations/*`.
    """
    router = APIRouter()

    def _resolve_runtime(request: Request) -> SystemRuntime:
        runtime = request.app.state.system_runtime
        if runtime is None:
            raise OperationError.service_unavailable("no system loaded")
        return runtime

    def _get_execution_op(request: Request) -> GetExecutionOperation:
        rt = _resolve_runtime(request)
        return GetExecutionOperation(
            runtime=rt, terminal_lookup=rt.execution_records.get_record,
        )

    def _list_executions_op(request: Request) -> ListExecutionsOperation:
        rt = _resolve_runtime(request)
        return ListExecutionsOperation(
            runtime=rt, terminal_list=rt.execution_records.list_records,
        )

    def _get_execution_detail_op(request: Request) -> GetExecutionDetailOperation:
        rt = _resolve_runtime(request)
        return GetExecutionDetailOperation(
            runtime=rt, terminal_detail_lookup=rt.execution_records.get_detail,
        )

    async def _empty_request_factory(_request: Request, model_cls: type[_R]) -> _R:
        return model_cls()

    async def _empty_system_info_request(request: Request) -> GetSystemInfoRequest:
        return await _empty_request_factory(request, GetSystemInfoRequest)

    async def _empty_list_executions_request(request: Request) -> ListExecutionsRequest:
        return await _empty_request_factory(request, ListExecutionsRequest)

    async def _empty_list_labware_request(request: Request) -> ListLabwareRequest:
        return await _empty_request_factory(request, ListLabwareRequest)

    async def _empty_list_devices_request(request: Request) -> ListDevicesRequest:
        return await _empty_request_factory(request, ListDevicesRequest)

    async def _empty_catalog_request(request: Request) -> _CatalogEmptyRequest:
        return await _empty_request_factory(request, _CatalogEmptyRequest)

    # -- Reference binding (kept for parity with a hosted REST surface) ------

    bind_orca_rest(
        router, path="/operations/system-info", method="GET",
        op_factory=lambda req: GetSystemInfoOperation(runtime=_resolve_runtime(req)),
        request_builder=_empty_system_info_request,
        response_model=GetSystemInfoResponse,
        tags=[RouteTag.SYSTEM],
    )

    # -- Thread-mutation Operations -------------------------------------------

    bind_orca_rest(
        router, path="/operations/pause", method="POST",
        op_factory=lambda req: PauseOperation(runtime=_resolve_runtime(req)),
        request_model=PauseRequest, response_model=PauseResponse,
        tags=[RouteTag.THREADS],
    )
    bind_orca_rest(
        router, path="/operations/resume", method="POST",
        op_factory=lambda req: ResumeOperation(runtime=_resolve_runtime(req)),
        request_model=ResumeRequest, response_model=ResumeResponse,
        tags=[RouteTag.THREADS],
    )
    bind_orca_rest(
        router, path="/operations/spawn-thread", method="POST",
        op_factory=lambda req: SpawnThreadOperation(runtime=_resolve_runtime(req)),
        request_model=SpawnThreadRequest, response_model=SpawnThreadResponse,
        tags=[RouteTag.THREADS],
    )
    bind_orca_rest(
        router, path="/operations/recover-thread", method="POST",
        op_factory=lambda req: RecoverThreadOperation(runtime=_resolve_runtime(req)),
        request_model=RecoverThreadRequest, response_model=RecoverThreadResponse,
        tags=[RouteTag.THREADS],
    )

    # Complex thread mutations.
    bind_orca_rest(
        router, path="/operations/skip-method", method="POST",
        op_factory=lambda req: SkipMethodOperation(runtime=_resolve_runtime(req)),
        request_model=SkipMethodRequest, response_model=_ThreadMutationResult,
        tags=[RouteTag.THREADS],
    )
    bind_orca_rest(
        router, path="/operations/abort-method", method="POST",
        op_factory=lambda req: AbortMethodOperation(runtime=_resolve_runtime(req)),
        request_model=AbortMethodRequest, response_model=_ThreadMutationResult,
        tags=[RouteTag.THREADS],
    )
    bind_orca_rest(
        router, path="/operations/skip-action", method="POST",
        op_factory=lambda req: SkipActionOperation(runtime=_resolve_runtime(req)),
        request_model=SkipActionRequest, response_model=_ThreadMutationResult,
        tags=[RouteTag.THREADS],
    )

    # Insert ops.
    bind_orca_rest(
        router, path="/operations/insert-method", method="POST",
        op_factory=lambda req: InsertMethodOperation(runtime=_resolve_runtime(req)),
        request_model=InsertMethodRequest, response_model=_ThreadMutationResult,
        tags=[RouteTag.THREADS],
    )
    bind_orca_rest(
        router, path="/operations/insert-action", method="POST",
        op_factory=lambda req: InsertActionOperation(runtime=_resolve_runtime(req)),
        request_model=InsertActionRequest, response_model=_ThreadMutationResult,
        tags=[RouteTag.THREADS],
    )

    # Replace a step. Pending target -> spliced. Errored target -> staged for
    # recovery (response carries staged_for_recovery + next_step).
    bind_orca_rest(
        router, path="/operations/replace-method", method="POST",
        op_factory=lambda req: ReplaceMethodOperation(runtime=_resolve_runtime(req)),
        request_model=ReplaceMethodRequest, response_model=ReplaceResult,
        tags=[RouteTag.THREADS],
    )
    bind_orca_rest(
        router, path="/operations/replace-action", method="POST",
        op_factory=lambda req: ReplaceActionOperation(runtime=_resolve_runtime(req)),
        request_model=ReplaceActionRequest, response_model=ReplaceResult,
        tags=[RouteTag.THREADS],
    )

    # -- Operator manual-step Operations --------------------------------------

    bind_orca_rest(
        router, path="/operations/list-pending-manual-steps", method="POST",
        op_factory=lambda req: ListPendingManualStepsOperation(runtime=_resolve_runtime(req)),
        request_model=ListPendingManualStepsRequest,
        response_model=ListPendingManualStepsResponse,
        tags=[RouteTag.MANUAL_STEPS],
    )
    bind_orca_rest(
        router, path="/operations/confirm-manual-step", method="POST",
        op_factory=lambda req: ConfirmManualStepOperation(runtime=_resolve_runtime(req)),
        request_model=ConfirmManualStepRequest,
        response_model=ConfirmManualStepResponse,
        tags=[RouteTag.MANUAL_STEPS],
    )

    # -- Execution-lifecycle Operations ---------------------------------------

    bind_orca_rest(
        router, path="/operations/close-execution", method="POST",
        op_factory=lambda req: CloseExecutionOperation(runtime=_resolve_runtime(req)),
        request_model=CloseExecutionRequest, response_model=CloseExecutionResponse,
        tags=[RouteTag.EXECUTIONS],
    )
    bind_orca_rest(
        router, path="/operations/get-execution", method="POST",
        op_factory=_get_execution_op,
        request_model=GetExecutionRequest, response_model=GetExecutionResponse,
        tags=[RouteTag.EXECUTIONS],
    )
    bind_orca_rest(
        router, path="/operations/list-executions", method="GET",
        op_factory=_list_executions_op,
        request_builder=_empty_list_executions_request,
        response_model=ListExecutionsResponse,
        tags=[RouteTag.EXECUTIONS],
    )
    bind_orca_rest(
        router, path="/operations/get-execution-detail", method="POST",
        op_factory=_get_execution_detail_op,
        request_model=GetExecutionDetailRequest,
        response_model=GetExecutionDetailResponse,
        tags=[RouteTag.EXECUTIONS],
    )
    bind_orca_rest(
        router, path="/operations/stop-execution", method="POST",
        op_factory=lambda req: StopExecutionOperation(runtime=_resolve_runtime(req)),
        request_model=StopExecutionRequest,
        response_model=StopExecutionResponse,
        tags=[RouteTag.EXECUTIONS],
    )
    bind_orca_rest(
        router, path="/operations/remove-execution", method="POST",
        op_factory=lambda req: RemoveExecutionOperation(runtime=_resolve_runtime(req)),
        request_model=RemoveExecutionRequest,
        response_model=RemoveExecutionResponse,
        tags=[RouteTag.EXECUTIONS],
    )

    # -- Labware Operations ---------------------------------------------------

    bind_orca_rest(
        router, path="/operations/get-labware-by-id", method="POST",
        op_factory=lambda req: GetLabwareByIdOperation(runtime=_resolve_runtime(req)),
        request_model=GetLabwareByIdRequest, response_model=GetLabwareResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/get-labware-by-barcode", method="POST",
        op_factory=lambda req: GetLabwareByBarcodeOperation(runtime=_resolve_runtime(req)),
        request_model=GetLabwareByBarcodeRequest, response_model=GetLabwareResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/list-labware", method="GET",
        op_factory=lambda req: ListLabwareOperation(runtime=_resolve_runtime(req)),
        request_builder=_empty_list_labware_request,
        response_model=ListLabwareResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/get-labware-history", method="POST",
        op_factory=lambda req: GetLabwareHistoryOperation(runtime=_resolve_runtime(req)),
        request_model=GetLabwareHistoryRequest, response_model=GetLabwareHistoryResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/edit-labware-location", method="POST",
        op_factory=lambda req: EditLabwareLocationOperation(runtime=_resolve_runtime(req)),
        request_model=EditLabwareLocationRequest, response_model=_MutationResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/edit-labware-barcode", method="POST",
        op_factory=lambda req: EditLabwareBarcodeOperation(runtime=_resolve_runtime(req)),
        request_model=EditLabwareBarcodeRequest, response_model=_MutationResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/set-labware-carry-override", method="POST",
        op_factory=lambda req: SetLabwareCarryOverrideOperation(
            runtime=_resolve_runtime(req),
        ),
        request_model=SetLabwareCarryOverrideRequest,
        response_model=CarryOverrideResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/clear-labware-carry-override", method="POST",
        op_factory=lambda req: ClearLabwareCarryOverrideOperation(
            runtime=_resolve_runtime(req),
        ),
        request_model=ClearLabwareCarryOverrideRequest,
        response_model=CarryOverrideResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/reset-labware-location", method="POST",
        op_factory=lambda req: ResetLabwareLocationOperation(runtime=_resolve_runtime(req)),
        request_model=ResetLabwareLocationRequest, response_model=_MutationResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/register-labware", method="POST",
        op_factory=lambda req: RegisterLabwareOperation(runtime=_resolve_runtime(req)),
        request_model=RegisterLabwareRequest, response_model=RegisterLabwareResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/get-labware-journey", method="POST",
        op_factory=lambda req: GetLabwareJourneyOperation(runtime=_resolve_runtime(req)),
        request_model=GetLabwareJourneyRequest, response_model=GetLabwareJourneyResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/get-well-volumes", method="POST",
        op_factory=lambda req: GetWellVolumesOperation(runtime=_resolve_runtime(req)),
        request_model=GetWellVolumesRequest, response_model=GetWellVolumesResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/set-well-volumes", method="POST",
        op_factory=lambda req: SetWellVolumesOperation(runtime=_resolve_runtime(req)),
        request_model=SetWellVolumesRequest, response_model=_MutationResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/mark-tips-used", method="POST",
        op_factory=lambda req: MarkTipsUsedOperation(runtime=_resolve_runtime(req)),
        request_model=MarkTipsUsedRequest, response_model=MarkTipsUsedResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/resolve-labware-contents", method="POST",
        op_factory=lambda req: ResolveContentsOperation(runtime=_resolve_runtime(req)),
        request_model=ResolveContentsRequest, response_model=ResolveContentsResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/get-tip-state", method="POST",
        op_factory=lambda req: GetTipStateOperation(runtime=_resolve_runtime(req)),
        request_model=GetTipStateRequest, response_model=GetTipStateResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/set-tip-state", method="POST",
        op_factory=lambda req: SetTipStateOperation(runtime=_resolve_runtime(req)),
        request_model=SetTipStateRequest, response_model=_MutationResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/release-mover-hold", method="POST",
        op_factory=lambda req: ReleaseMoverHoldOperation(runtime=_resolve_runtime(req)),
        request_model=ReleaseMoverHoldRequest, response_model=ReleaseMoverHoldResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/confirm-tip-state", method="POST",
        op_factory=lambda req: ConfirmTipStateOperation(runtime=_resolve_runtime(req)),
        request_model=ConfirmTipStateRequest, response_model=_MutationResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/confirm-well-volumes", method="POST",
        op_factory=lambda req: ConfirmWellVolumesOperation(runtime=_resolve_runtime(req)),
        request_model=ConfirmWellVolumesRequest, response_model=_MutationResponse,
        tags=[RouteTag.LABWARE],
    )

    # -- Device introspection Operations --------------------------------------

    bind_orca_rest(
        router, path="/operations/list-devices", method="GET",
        op_factory=lambda req: ListDevicesOperation(runtime=_resolve_runtime(req)),
        request_builder=_empty_list_devices_request,
        response_model=ListDevicesResponse,
        tags=[RouteTag.DEVICES],
    )
    bind_orca_rest(
        router, path="/operations/get-device-status", method="POST",
        op_factory=lambda req: GetDeviceStatusOperation(runtime=_resolve_runtime(req)),
        request_model=GetDeviceStatusRequest, response_model=GetDeviceStatusResponse,
        tags=[RouteTag.DEVICES],
    )
    bind_orca_rest(
        router, path="/operations/initialize-device", method="POST",
        op_factory=lambda req: InitializeDeviceOperation(runtime=_resolve_runtime(req)),
        request_model=InitializeDeviceRequest, response_model=InitializeDeviceResponse,
        tags=[RouteTag.DEVICES],
    )
    bind_orca_rest(
        router, path="/operations/connect-device", method="POST",
        op_factory=lambda req: ConnectDeviceOperation(runtime=_resolve_runtime(req)),
        request_model=ConnectDeviceRequest, response_model=ConnectDeviceResponse,
        tags=[RouteTag.DEVICES],
    )
    bind_orca_rest(
        router, path="/operations/disconnect-device", method="POST",
        op_factory=lambda req: DisconnectDeviceOperation(runtime=_resolve_runtime(req)),
        request_model=DisconnectDeviceRequest, response_model=DisconnectDeviceResponse,
        tags=[RouteTag.DEVICES],
    )
    bind_orca_rest(
        router, path="/operations/reconcile-deck", method="POST",
        op_factory=lambda req: ReconcileDeckOperation(runtime=_resolve_runtime(req)),
        request_model=ReconcileDeckRequest, response_model=ReconcileDeckResponse,
        tags=[RouteTag.DEVICES],
    )
    bind_orca_rest(
        router, path="/operations/compare-deck", method="POST",
        op_factory=lambda req: CompareDeckOperation(runtime=_resolve_runtime(req)),
        request_model=CompareDeckRequest, response_model=CompareDeckResponse,
        tags=[RouteTag.DEVICES],
    )
    bind_orca_rest(
        router, path="/operations/unsettled", method="POST",
        op_factory=lambda req: UnsettledStateOperation(runtime=_resolve_runtime(req)),
        request_model=UnsettledStateRequest, response_model=UnsettledStateResponse,
        tags=[RouteTag.LABWARE],
    )
    bind_orca_rest(
        router, path="/operations/get-mounted-tips", method="POST",
        op_factory=lambda req: GetMountedTipsOperation(runtime=_resolve_runtime(req)),
        request_model=GetMountedTipsRequest, response_model=GetMountedTipsResponse,
        tags=[RouteTag.DEVICES],
    )
    bind_orca_rest(
        router, path="/operations/set-mounted-tips", method="POST",
        op_factory=lambda req: SetMountedTipsOperation(runtime=_resolve_runtime(req)),
        request_model=SetMountedTipsRequest, response_model=MountedTipsMutationResponse,
        tags=[RouteTag.DEVICES],
    )
    bind_orca_rest(
        router, path="/operations/confirm-mounted-tips", method="POST",
        op_factory=lambda req: ConfirmMountedTipsOperation(runtime=_resolve_runtime(req)),
        request_model=ConfirmMountedTipsRequest, response_model=MountedTipsMutationResponse,
        tags=[RouteTag.DEVICES],
    )
    bind_orca_rest(
        router, path="/operations/take-device-control", method="POST",
        op_factory=lambda req: TakeDeviceControlOperation(runtime=_resolve_runtime(req)),
        request_model=TakeDeviceControlRequest,
        response_model=TakeDeviceControlResponse,
        tags=[RouteTag.DEVICES],
    )
    bind_orca_rest(
        router, path="/operations/release-device-control", method="POST",
        op_factory=lambda req: ReleaseDeviceControlOperation(
            runtime=_resolve_runtime(req),
        ),
        request_model=ReleaseDeviceControlRequest,
        response_model=ReleaseDeviceControlResponse,
        tags=[RouteTag.DEVICES],
    )
    bind_orca_rest(
        router, path="/operations/clear-device-fault", method="POST",
        op_factory=lambda req: ClearDeviceFaultOperation(
            runtime=_resolve_runtime(req),
        ),
        request_model=ClearDeviceFaultRequest,
        response_model=ClearDeviceFaultResponse,
        tags=[RouteTag.DEVICES],
    )

    # -- Catalog reads ----------------------------------------------------------

    bind_orca_rest(
        router, path="/operations/list-workflows", method="GET",
        op_factory=lambda req: ListWorkflowsOperation(runtime=_resolve_runtime(req)),
        request_builder=_empty_catalog_request,
        response_model=ListWorkflowsResponse,
        tags=[RouteTag.CATALOG],
    )
    bind_orca_rest(
        router, path="/operations/list-methods", method="GET",
        op_factory=lambda req: ListMethodsOperation(runtime=_resolve_runtime(req)),
        request_builder=_empty_catalog_request,
        response_model=ListMethodsResponse,
        tags=[RouteTag.CATALOG],
    )
    bind_orca_rest(
        router, path="/operations/list-thread-templates", method="GET",
        op_factory=lambda req: ListThreadTemplatesOperation(runtime=_resolve_runtime(req)),
        request_builder=_empty_catalog_request,
        response_model=ListThreadTemplatesResponse,
        tags=[RouteTag.CATALOG],
    )
    bind_orca_rest(
        router, path="/operations/list-locations", method="GET",
        op_factory=lambda req: ListLocationsOperation(runtime=_resolve_runtime(req)),
        request_builder=_empty_catalog_request,
        response_model=ListLocationsResponse,
        tags=[RouteTag.CATALOG],
    )
    bind_orca_rest(
        router, path="/operations/get-workflow", method="POST",
        op_factory=lambda req: GetWorkflowOperation(runtime=_resolve_runtime(req)),
        request_model=GetWorkflowRequest, response_model=GetWorkflowResponse,
        tags=[RouteTag.CATALOG],
    )
    bind_orca_rest(
        router, path="/operations/get-method", method="POST",
        op_factory=lambda req: GetMethodOperation(runtime=_resolve_runtime(req)),
        request_model=GetMethodRequest, response_model=GetMethodResponse,
        tags=[RouteTag.CATALOG],
    )

    # -- Ops-history reads (per-execution list + cross-execution search) ------

    bind_orca_rest(
        router, path="/operations/list-ops-history", method="POST",
        op_factory=lambda req: ListOpsHistoryOperation(runtime=_resolve_runtime(req)),
        request_model=ListOpsHistoryRequest, response_model=ListOpsHistoryResponse,
        tags=[RouteTag.OPS_HISTORY],
    )
    bind_orca_rest(
        router, path="/operations/search-ops-history", method="POST",
        op_factory=lambda req: SearchOpsHistoryOperation(runtime=_resolve_runtime(req)),
        request_model=SearchOpsHistoryRequest, response_model=SearchOpsHistoryResponse,
        tags=[RouteTag.OPS_HISTORY],
    )

    return router
