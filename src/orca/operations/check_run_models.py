"""Wire shapes for reading a check run's own store."""

from pydantic import BaseModel, ConfigDict

from orca.state.provenance import Provenance
from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag


class GetCheckRunLabwareRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str


class CheckRunLabwareModel(BaseModel):
    """One labware in a check run's store, where the run left it, and what it holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    name: str
    template_name: str
    labware_type: str
    barcode: str | None
    location: str
    well_volumes: dict[str, float]
    provenance: Provenance


class GetCheckRunLabwareResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str
    labware: list[CheckRunLabwareModel]


GET_CHECK_RUN_LABWARE_ROUTE = Route(
    GetCheckRunLabwareRequest,
    GetCheckRunLabwareResponse,
    RestBinding(
        method="GET", path="/executions/{execution_id}/check-run-labware", tag=RouteTag.LABWARE,
    ),
)
