"""Request and response models for the catalog Operations, importable without the engine."""

from pydantic import BaseModel, ConfigDict

from orca.operations.wire_models import (
    MethodTemplateDTO,
    ThreadTemplateDTO,
    WorkflowTemplateDTO,
)
from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag


class EmptyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NoContent(BaseModel):
    """The Response of an Operation bound with status 204."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ListWorkflowsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    workflows: list[WorkflowTemplateDTO]


class ListMethodsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    methods: list[MethodTemplateDTO]


class ListThreadTemplatesResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    threads: list[ThreadTemplateDTO]


class GetWorkflowRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str


class GetWorkflowResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    workflow: WorkflowTemplateDTO


class GetMethodRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    workflow_name: str | None = None


class GetMethodResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    method: MethodTemplateDTO


LIST_WORKFLOWS_ROUTE = Route(
    EmptyRequest,
    ListWorkflowsResponse,
    RestBinding(
        method="GET", path="/workflows", tag=RouteTag.CATALOG,
    ),
)


LIST_METHODS_ROUTE = Route(
    EmptyRequest,
    ListMethodsResponse,
    RestBinding(
        method="GET", path="/methods", tag=RouteTag.CATALOG,
    ),
)


LIST_THREAD_TEMPLATES_ROUTE = Route(
    EmptyRequest,
    ListThreadTemplatesResponse,
    RestBinding(
        method="GET", path="/thread-templates", tag=RouteTag.CATALOG,
    ),
)


GET_WORKFLOW_ROUTE = Route(
    GetWorkflowRequest,
    GetWorkflowResponse,
    RestBinding(
        method="GET", path="/workflows/{name}", tag=RouteTag.CATALOG,
    ),
)


GET_METHOD_ROUTE = Route(
    GetMethodRequest,
    GetMethodResponse,
    RestBinding(
        method="GET", path="/methods/{name}", tag=RouteTag.CATALOG,
    ),
)
