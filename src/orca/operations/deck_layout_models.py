"""Deck layout wire models and routes."""

from cheshire_drivers.liquid_handler_models import DeckLayoutConfig
from pydantic import BaseModel, ConfigDict, Field, RootModel

from orca.operations.wire_models import DeckLayoutDTO, DeckLayoutSummaryDTO
from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag
from orca.operations.catalog_models import NoContent


_REBUILD_NOTE = (
    "Takes effect on the next reload; the running liquid handler keeps the "
    "previously resolved layout until then."
)


class DeckLayoutResponse(DeckLayoutDTO):
    """The stored layout, plus when an edit reaches the running liquid handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rebuild_note: str = _REBUILD_NOTE


class DeckLayoutSummaryListResponse(RootModel[list[DeckLayoutSummaryDTO]]):
    model_config = ConfigDict(frozen=True)


class ListDeckLayoutsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str | None = Field(
        default=None,
        description="Only this liquid handler's layouts; 404 `liquid_handler_not_found` when it is not one.",
    )


class GetDeckLayoutRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    name: str


class UpdateDeckLayoutRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    name: str
    config: DeckLayoutConfig
    reason: str | None = None


class DeleteDeckLayoutRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    name: str
    reason: str | None = None


LIST_DECK_LAYOUTS_ROUTE = Route(
    ListDeckLayoutsRequest,
    DeckLayoutSummaryListResponse,
    RestBinding(
        method="GET", path="/deck-layouts", tag=RouteTag.DECK_LAYOUTS,
    ),
)


GET_DECK_LAYOUT_ROUTE = Route(
    GetDeckLayoutRequest,
    DeckLayoutResponse,
    RestBinding(
        method="GET", path="/deck-layouts/{device_id}/{name}", tag=RouteTag.DECK_LAYOUTS,
    ),
)


UPDATE_DECK_LAYOUT_ROUTE = Route(
    UpdateDeckLayoutRequest,
    DeckLayoutResponse,
    RestBinding(
        method="PUT", path="/deck-layouts/{device_id}/{name}", tag=RouteTag.DECK_LAYOUTS,
    ),
)


DELETE_DECK_LAYOUT_ROUTE = Route(
    DeleteDeckLayoutRequest,
    NoContent,
    RestBinding(
        method="DELETE", path="/deck-layouts/{device_id}/{name}", tag=RouteTag.DECK_LAYOUTS,
        status_code=204,
    ),
)
