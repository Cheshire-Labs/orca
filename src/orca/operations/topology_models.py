"""Topology wire models and routes."""

from pydantic import BaseModel, ConfigDict, RootModel

from orca.operations.wire_models import TopologySourceResponse, TopologyViewResponse
from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag


class GetTopologyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: bool = False


class TopologyAnswer(RootModel[TopologyViewResponse | TopologySourceResponse]):
    model_config = ConfigDict(frozen=True)


GET_TOPOLOGY_ROUTE = Route(
    GetTopologyRequest,
    TopologyAnswer,
    RestBinding(
        method="GET", path="/topology", tag=RouteTag.TOPOLOGY,
    ),
)
