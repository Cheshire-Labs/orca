"""Catalog read Operations.

Surfaces the `IRegistryFacade` catalog read methods that operators / AI
agents call to inventory the loaded system:
- ListWorkflows / GetWorkflow
- ListMethods / GetMethod
- ListThreadTemplates
- ListLocations
- ListPlugins

All read-only; no Scope dispatch; no @dangerous wrap.

Wire mirrors: the source snapshots are frozen dataclasses on
``orca.runtime.status_models`` (``WorkflowTemplateSnapshot``,
``MethodTemplateSnapshot``, ``ThreadTemplateSnapshot``,
``LocationSnapshot``). The Operations expose them as the typed
``orca.daemon.schemas`` DTOs of the same shape; ``dataclasses.asdict``
+ ``model_validate`` does the conversion field-by-field, so a snapshot
field rename surfaces at type-check time instead of silently dropping.
"""

import dataclasses
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from orca.daemon.schemas import (
    LocationDTO,
    MethodTemplateDTO,
    ThreadTemplateDTO,
    WorkflowTemplateDTO,
)
from orca.operations._protocol import OperationError
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.status_models import (
    LocationSnapshot,
    MethodTemplateSnapshot,
    ThreadTemplateSnapshot,
    WorkflowTemplateSnapshot,
)


# -- Generic empty request --------------------------------------------------


class _EmptyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# -- Snapshot -> DTO converters (typed; one per source dataclass) -----------


def _workflow_to_dto(snap: WorkflowTemplateSnapshot) -> WorkflowTemplateDTO:
    return WorkflowTemplateDTO.model_validate(dataclasses.asdict(snap))


def _method_to_dto(snap: MethodTemplateSnapshot) -> MethodTemplateDTO:
    return MethodTemplateDTO.model_validate(dataclasses.asdict(snap))


def _thread_to_dto(snap: ThreadTemplateSnapshot) -> ThreadTemplateDTO:
    return ThreadTemplateDTO.model_validate(dataclasses.asdict(snap))


def _location_to_dto(snap: LocationSnapshot) -> LocationDTO:
    return LocationDTO.model_validate(dataclasses.asdict(snap))


# -- ListWorkflows ----------------------------------------------------------


class ListWorkflowsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    workflows: list[WorkflowTemplateDTO]


class ListWorkflowsOperation:
    Request: ClassVar[type[BaseModel]] = _EmptyRequest
    Response: ClassVar[type[BaseModel]] = ListWorkflowsResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: _EmptyRequest) -> ListWorkflowsResponse:
        del req
        snaps = self._runtime.registry.list_workflow_templates()
        return ListWorkflowsResponse(
            workflows=[_workflow_to_dto(s) for s in snaps],
        )


# -- ListMethods ------------------------------------------------------------


class ListMethodsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    methods: list[MethodTemplateDTO]


class ListMethodsOperation:
    Request: ClassVar[type[BaseModel]] = _EmptyRequest
    Response: ClassVar[type[BaseModel]] = ListMethodsResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: _EmptyRequest) -> ListMethodsResponse:
        del req
        snaps = self._runtime.registry.list_method_templates()
        return ListMethodsResponse(
            methods=[_method_to_dto(s) for s in snaps],
        )


# -- ListThreadTemplates ----------------------------------------------------


class ListThreadTemplatesResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    threads: list[ThreadTemplateDTO]


class ListThreadTemplatesOperation:
    Request: ClassVar[type[BaseModel]] = _EmptyRequest
    Response: ClassVar[type[BaseModel]] = ListThreadTemplatesResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: _EmptyRequest) -> ListThreadTemplatesResponse:
        del req
        snaps = self._runtime.registry.list_thread_templates()
        return ListThreadTemplatesResponse(
            threads=[_thread_to_dto(s) for s in snaps],
        )


# -- ListLocations ----------------------------------------------------------


class ListLocationsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    locations: list[LocationDTO]


class ListLocationsOperation:
    Request: ClassVar[type[BaseModel]] = _EmptyRequest
    Response: ClassVar[type[BaseModel]] = ListLocationsResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: _EmptyRequest) -> ListLocationsResponse:
        del req
        snaps = self._runtime.registry.list_locations()
        return ListLocationsResponse(
            locations=[_location_to_dto(s) for s in snaps],
        )


# -- GetWorkflow -------------------------------------------------------------


class GetWorkflowRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str


class GetWorkflowResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    workflow: WorkflowTemplateDTO


class GetWorkflowOperation:
    """Fetch a single workflow template summary by name.

    Sources the snapshot from ``list_workflow_templates`` so the wire
    shape mirrors the list endpoint exactly. The registry's
    ``get_workflow_template`` returns the runtime ``WorkflowTemplate``
    object (used by execution submit + insert-method paths); the read
    surface uses the snapshot instead so the response is the
    serializable mirror.
    """

    Request: ClassVar[type[BaseModel]] = GetWorkflowRequest
    Response: ClassVar[type[BaseModel]] = GetWorkflowResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: GetWorkflowRequest) -> GetWorkflowResponse:
        snaps = self._runtime.registry.list_workflow_templates()
        match = next((s for s in snaps if s.name == req.name), None)
        if match is None:
            raise OperationError.not_found(
                f"workflow {req.name!r} not found", workflow_name=req.name,
            )
        return GetWorkflowResponse(workflow=_workflow_to_dto(match))


# -- GetMethod --------------------------------------------------------------


class GetMethodRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    workflow_name: str | None = None


class GetMethodResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    method: MethodTemplateDTO


class GetMethodOperation:
    """Fetch a single method template summary by name.

    Sources the snapshot from ``list_method_templates`` so the wire
    shape mirrors the list endpoint exactly. Method-read is kept
    scope-minimal (summary only, no source) so the engine doesn't take
    a dependency on a git service it doesn't own; source reads go through
    the separate git-files surface each host exposes.
    """

    Request: ClassVar[type[BaseModel]] = GetMethodRequest
    Response: ClassVar[type[BaseModel]] = GetMethodResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: GetMethodRequest) -> GetMethodResponse:
        snaps = [s for s in self._runtime.registry.list_method_templates() if s.name == req.name]
        if req.workflow_name is not None:
            snaps = [s for s in snaps if s.workflow_name == req.workflow_name]
        if not snaps:
            raise OperationError.not_found(
                f"method {req.name!r} not found", method_name=req.name,
            )
        if len(snaps) > 1:
            owners = sorted(s.workflow_name for s in snaps)
            raise OperationError.invalid_input(
                f"method {req.name!r} is defined by multiple workflows "
                f"{owners}; pass workflow_name to disambiguate",
            )
        return GetMethodResponse(method=_method_to_dto(snaps[0]))
