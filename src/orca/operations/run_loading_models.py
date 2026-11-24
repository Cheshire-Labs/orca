"""Wire models for loading the labware a run needs before it starts."""

from pydantic import BaseModel, ConfigDict, Field
from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag


class RunLabwareItem(BaseModel):
    """One labware a workflow's thread starts on, as the operator put it down."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    thread_template_name: str
    volume_ul: float | None = Field(
        default=None, ge=0,
        description="uL in every well of a plate, or in a trough's pool. None leaves the workflow's own figure.",
    )


class LoadRunLabwareRequest(BaseModel):
    """Record the labware a workflow needs before a run as loaded on each thread's start position."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    workflow_name: str
    items: list[RunLabwareItem] = Field(
        default_factory=list,
        description="The threads to load, each with its volume. Empty loads every thread the workflow needs.",
    )
    clear_all_first: bool = Field(
        default=False,
        description="Clear every labware record first, reused labware included. "
                    "Otherwise whatever is on an item's start position is taken off the record.",
    )


class LoadedRunItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    thread_template_name: str
    labware_id: str


class NotLoadedRunItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    thread_template_name: str
    code: str
    message: str


class LoadRunLabwareResponse(BaseModel):
    """Each thread is loaded, kept or not loaded; one failing does not stop the rest."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    cleared: list[str]
    loaded: list[LoadedRunItem]
    kept: list[LoadedRunItem] = Field(
        description="Reused labware of the thread's template already on its start position, left as it is.",
    )
    not_loaded: list[NotLoadedRunItem]


LOAD_RUN_LABWARE_ROUTE = Route(
    LoadRunLabwareRequest,
    LoadRunLabwareResponse,
    RestBinding(
        method="POST", path="/workflows/{workflow_name}/load-run-labware", tag=RouteTag.LABWARE,
        carries_blockers=True,
        drives_device=True,
    ),
)
