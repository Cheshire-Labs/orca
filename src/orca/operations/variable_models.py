"""Wire shapes for the variable Operations. Engine-free, so the CLI parses with them."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from orca.variables.resolution import VariableSource
from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag

OptionValueJson = str | int | float | bool


class _Request(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _Reason(_Request):
    reason: str | None = Field(default=None, description="Lands on the audit row.")


class VariableSetRequest(_Reason):
    """The JSON body of every variable write."""

    value: OptionValueJson


class GetGlobalVariableRequest(_Request):
    name: str = Field(description="The bare name, without the `global.` prefix.")


class SetGlobalVariableRequest(VariableSetRequest):
    name: str


class UnsetGlobalVariableRequest(_Reason):
    name: str


class ListAllVariablesRequest(_Request):
    pass


class ListVariablesRequest(_Request):
    execution_id: str


class GetVariableRequest(_Request):
    execution_id: str
    name: str
    submission_id: str | None = Field(
        default=None,
        description=(
            "Resolve as this submission's threads do. Omit for the value "
            "submissions without an override resolve."
        ),
    )


class SetVariableRequest(VariableSetRequest):
    execution_id: str
    name: str


class UnsetVariableRequest(_Reason):
    execution_id: str
    name: str


class SetSubmissionVariableRequest(VariableSetRequest):
    execution_id: str
    submission_id: str
    name: str


class UnsetSubmissionVariableRequest(_Reason):
    execution_id: str
    submission_id: str
    name: str


class CrossExecVariableDTO(BaseModel):
    """One row of the cross-execution variable listing."""

    execution_id: str
    name: str
    value: OptionValueJson


class VariableRowsResponse(RootModel[list[CrossExecVariableDTO]]):
    """Every variable of every execution the runtime holds."""
    model_config = ConfigDict(frozen=True)


class VariablesListResponse(RootModel[dict[str, OptionValueJson]]):
    """One execution's merged view: name -> value."""
    model_config = ConfigDict(frozen=True)


class SubmissionOverrideDTO(BaseModel):
    """One submission's own value for a variable."""

    submission_id: str
    value: OptionValueJson


class VariableValue(BaseModel):
    """One variable's value.

    `execution_id` is null for a global. `value` is what a thread whose
    submission holds no override resolves. `value` and `source` are null when
    only submissions hold the name. `overrides` lists each submission that
    resolves something else, with its value.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    execution_id: str | None = None
    value: OptionValueJson | None = None
    source: VariableSource | None = None
    overrides: list[SubmissionOverrideDTO] = Field(default_factory=list)


class VariableSetResponse(BaseModel):
    """`shadowed_by` names submissions whose own value still outranks the write."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["set"] = "set"
    name: str
    value: OptionValueJson
    shadowed_by: list[str] = Field(default_factory=list)


class VariableUnsetResponse(BaseModel):
    """`existed` says whether the unset removed a value, not whether the name still resolves."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["unset"] = "unset"
    existed: bool


GET_GLOBAL_VARIABLE_ROUTE = Route(
    GetGlobalVariableRequest,
    VariableValue,
    RestBinding(
        method="GET", path="/variables/global/{name}", tag=RouteTag.VARIABLES,
    ),
)


SET_GLOBAL_VARIABLE_ROUTE = Route(
    SetGlobalVariableRequest,
    VariableSetResponse,
    RestBinding(
        method="PUT", path="/variables/global/{name}", tag=RouteTag.VARIABLES,
    ),
)


UNSET_GLOBAL_VARIABLE_ROUTE = Route(
    UnsetGlobalVariableRequest,
    VariableUnsetResponse,
    RestBinding(
        method="DELETE", path="/variables/global/{name}", tag=RouteTag.VARIABLES,
    ),
)


LIST_ALL_VARIABLES_ROUTE = Route(
    ListAllVariablesRequest,
    VariableRowsResponse,
    RestBinding(
        method="GET", path="/variables", tag=RouteTag.VARIABLES,
    ),
)


LIST_VARIABLES_ROUTE = Route(
    ListVariablesRequest,
    VariablesListResponse,
    RestBinding(
        method="GET", path="/executions/{execution_id}/variables", tag=RouteTag.VARIABLES,
    ),
)


GET_VARIABLE_ROUTE = Route(
    GetVariableRequest,
    VariableValue,
    RestBinding(
        method="GET", path="/executions/{execution_id}/variables/{name}", tag=RouteTag.VARIABLES,
    ),
)


SET_VARIABLE_ROUTE = Route(
    SetVariableRequest,
    VariableSetResponse,
    RestBinding(
        method="PUT", path="/executions/{execution_id}/variables/{name}", tag=RouteTag.VARIABLES,
    ),
)


UNSET_VARIABLE_ROUTE = Route(
    UnsetVariableRequest,
    VariableUnsetResponse,
    RestBinding(
        method="DELETE", path="/executions/{execution_id}/variables/{name}", tag=RouteTag.VARIABLES,
    ),
)


SET_SUBMISSION_VARIABLE_ROUTE = Route(
    SetSubmissionVariableRequest,
    VariableSetResponse,
    RestBinding(
        method="PUT", path="/executions/{execution_id}/submissions/{submission_id}/variables/{name}",
        tag=RouteTag.VARIABLES,
    ),
)


UNSET_SUBMISSION_VARIABLE_ROUTE = Route(
    UnsetSubmissionVariableRequest,
    VariableUnsetResponse,
    RestBinding(
        method="DELETE", path="/executions/{execution_id}/submissions/{submission_id}/variables/{name}",
        tag=RouteTag.VARIABLES,
    ),
)
