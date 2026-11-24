from orca.operations.catalog_models import (
    GET_METHOD_ROUTE,
    GET_WORKFLOW_ROUTE,
    LIST_METHODS_ROUTE,
    LIST_THREAD_TEMPLATES_ROUTE,
    LIST_WORKFLOWS_ROUTE,
)
"""Catalog read Operations.

Surfaces the `IRegistryFacade` catalog read methods that operators / AI
agents call to inventory the loaded system:
- ListWorkflows / GetWorkflow
- ListMethods / GetMethod
- ListThreadTemplates

All read-only; no Scope dispatch; no @dangerous wrap.

Wire mirrors: the source snapshots are frozen dataclasses on
``orca.runtime.status_models`` (``WorkflowTemplateSnapshot``,
``MethodTemplateSnapshot``, ``ThreadTemplateSnapshot``,
``LocationSnapshot``). The Operations expose them as the typed
``orca.operations.wire_models`` DTOs of the same shape; ``dataclasses.asdict``
+ ``model_validate`` does the conversion field-by-field, so a snapshot
field rename surfaces at type-check time instead of silently dropping.
"""

from orca.operations.binding import RestBinding
import dataclasses
from typing import ClassVar

from pydantic import BaseModel

from orca.operations.wire_models import (
    MethodTemplateDTO,
    ThreadTemplateDTO,
    WorkflowTemplateDTO,
)
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.operations.catalog_models import (
    EmptyRequest,
    GetMethodRequest,
    GetMethodResponse,
    GetWorkflowRequest,
    GetWorkflowResponse,
    ListMethodsResponse,
    ListThreadTemplatesResponse,
    ListWorkflowsResponse,
)
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.status_models import (
    MethodTemplateSnapshot,
    ThreadTemplateSnapshot,
    WorkflowTemplateSnapshot,
)


# -- Snapshot -> DTO converters (typed; one per source dataclass) -----------


def _workflow_to_dto(snap: WorkflowTemplateSnapshot) -> WorkflowTemplateDTO:
    return WorkflowTemplateDTO.model_validate(dataclasses.asdict(snap))


def _method_to_dto(snap: MethodTemplateSnapshot) -> MethodTemplateDTO:
    return MethodTemplateDTO.model_validate(dataclasses.asdict(snap))


def _thread_to_dto(snap: ThreadTemplateSnapshot) -> ThreadTemplateDTO:
    return ThreadTemplateDTO.model_validate(dataclasses.asdict(snap))


# -- ListWorkflows ----------------------------------------------------------


class ListWorkflowsOperation:
    Request: ClassVar[type[BaseModel]] = LIST_WORKFLOWS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_WORKFLOWS_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_WORKFLOWS_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: EmptyRequest) -> ListWorkflowsResponse:
        del req
        snaps = self._runtime.registry.list_workflow_templates()
        return ListWorkflowsResponse(
            workflows=[_workflow_to_dto(s) for s in snaps],
        )


# -- ListMethods ------------------------------------------------------------


class ListMethodsOperation:
    Request: ClassVar[type[BaseModel]] = LIST_METHODS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_METHODS_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_METHODS_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: EmptyRequest) -> ListMethodsResponse:
        del req
        snaps = self._runtime.registry.list_method_templates()
        return ListMethodsResponse(
            methods=[_method_to_dto(s) for s in snaps],
        )


# -- ListThreadTemplates ----------------------------------------------------


class ListThreadTemplatesOperation:
    Request: ClassVar[type[BaseModel]] = LIST_THREAD_TEMPLATES_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_THREAD_TEMPLATES_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_THREAD_TEMPLATES_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: EmptyRequest) -> ListThreadTemplatesResponse:
        del req
        snaps = self._runtime.registry.list_thread_templates()
        return ListThreadTemplatesResponse(
            threads=[_thread_to_dto(s) for s in snaps],
        )


# -- GetWorkflow -------------------------------------------------------------


class GetWorkflowOperation:
    """Fetch a single workflow template summary by name.

    Sources the snapshot from ``list_workflow_templates`` so the wire
    shape mirrors the list endpoint exactly. The registry's
    ``get_workflow_template`` returns the runtime ``WorkflowTemplate``
    object (used by execution submit + insert-method paths); the read
    surface uses the snapshot instead so the response is the
    serializable mirror.
    """

    Request: ClassVar[type[BaseModel]] = GET_WORKFLOW_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_WORKFLOW_ROUTE.response
    binding: ClassVar[RestBinding] = GET_WORKFLOW_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: GetWorkflowRequest) -> GetWorkflowResponse:
        snaps = self._runtime.registry.list_workflow_templates()
        match = next((s for s in snaps if s.name == req.name), None)
        if match is None:
            raise OperationError.of(
                ErrorCode.WORKFLOW_NOT_FOUND,
                f"workflow {req.name!r} not found", workflow_name=req.name,
            )
        return GetWorkflowResponse(workflow=_workflow_to_dto(match))


# -- GetMethod --------------------------------------------------------------


class GetMethodOperation:
    """Fetch a single method template summary by name.

    Sources the snapshot from ``list_method_templates`` so the wire
    shape mirrors the list endpoint exactly. Method-read is kept
    scope-minimal (summary only, no source) so the engine doesn't take
    a dependency on a git service it doesn't own; source reads go through
    the separate git-files surface each host exposes.
    """

    Request: ClassVar[type[BaseModel]] = GET_METHOD_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_METHOD_ROUTE.response
    binding: ClassVar[RestBinding] = GET_METHOD_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: GetMethodRequest) -> GetMethodResponse:
        snaps = [s for s in self._runtime.registry.list_method_templates() if s.name == req.name]
        if req.workflow_name is not None:
            snaps = [s for s in snaps if s.workflow_name == req.workflow_name]
        if not snaps:
            raise OperationError.of(
                ErrorCode.METHOD_NOT_FOUND,
                f"method {req.name!r} not found", method_name=req.name,
            )
        if len(snaps) > 1:
            owners = sorted(s.workflow_name for s in snaps)
            raise OperationError.validation_error(
                f"method {req.name!r} is defined by multiple workflows "
                f"{owners}; pass workflow_name to disambiguate",
            )
        return GetMethodResponse(method=_method_to_dto(snaps[0]))


CATALOG_OPERATIONS = (
    ListWorkflowsOperation,
    ListMethodsOperation,
    ListThreadTemplatesOperation,
    GetWorkflowOperation,
    GetMethodOperation,
)
