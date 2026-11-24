"""Wire models of the labware catalog routes."""

from typing import Literal
from typing_extensions import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from orca.runtime.labware_catalog_store import LabwareCatalogEntry as CatalogRow
from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag
from orca.operations.catalog_models import NoContent

LabwareCategory = Literal["plate", "tip_rack", "trough", "tube", "carrier"]
LabwareSource = Literal["plr_seed", "operator_custom"]


def _narrow_source(source: str) -> LabwareSource:
    if source == "plr_seed":
        return "plr_seed"
    if source == "operator_custom":
        return "operator_custom"
    raise ValueError(f"catalog row carries unknown source {source!r}")


class LabwareCatalogRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    category: LabwareCategory | None = Field(
        default=None, description="Filter to one category: plate, tip_rack, trough, tube, carrier.",
    )


class LabwareEntryRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    labware_type: str = Field(description="Catalog labware_type identifier")


class LabwareCreateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    labware_type: str
    display_name: str
    category: LabwareCategory
    vendor: str | None = None
    plr_class_name: str | None = None
    geometry: dict[str, JsonValue]


class LabwareUpdateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    labware_type: str = Field(description="Catalog labware_type identifier")
    display_name: str
    category: LabwareCategory
    vendor: str | None = None
    plr_class_name: str | None = None
    geometry: dict[str, JsonValue]


class LabwareCatalogEntry(BaseModel):
    """One catalog row with its full geometry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    labware_type: str
    display_name: str
    category: str
    vendor: str | None
    source: LabwareSource
    geometry: dict[str, JsonValue]
    plr_class_name: str | None

    @classmethod
    def from_row(cls, row: CatalogRow) -> Self:
        return cls(
            labware_type=row.labware_type,
            display_name=row.display_name,
            category=row.category,
            vendor=row.vendor,
            source=_narrow_source(row.source),
            geometry=dict(row.geometry),
            plr_class_name=row.plr_class_name,
        )


class LabwareCatalogSummary(BaseModel):
    """One list row. A 384-well geometry is about 77 KB, so the list carries none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    labware_type: str
    display_name: str
    category: str
    vendor: str | None
    source: LabwareSource
    plr_class_name: str | None

    @classmethod
    def from_row(cls, row: CatalogRow) -> Self:
        return cls(
            labware_type=row.labware_type,
            display_name=row.display_name,
            category=row.category,
            vendor=row.vendor,
            source=_narrow_source(row.source),
            plr_class_name=row.plr_class_name,
        )


class LabwareCatalogResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    labware: list[LabwareCatalogSummary]


GET_LABWARE_CATALOG_ROUTE = Route(
    LabwareCatalogRequest,
    LabwareCatalogResponse,
    RestBinding(method="GET", path="/labware-catalog", tag=RouteTag.LABWARE),
)


GET_LABWARE_ENTRY_ROUTE = Route(
    LabwareEntryRequest,
    LabwareCatalogEntry,
    RestBinding(
        method="GET", path="/labware-catalog/{labware_type}", tag=RouteTag.LABWARE,
    ),
)


ADD_LABWARE_ENTRY_ROUTE = Route(
    LabwareCreateRequest,
    LabwareCatalogEntry,
    RestBinding(
        method="POST", path="/labware-catalog", tag=RouteTag.LABWARE, status_code=201,
    ),
)


UPDATE_LABWARE_ENTRY_ROUTE = Route(
    LabwareUpdateRequest,
    LabwareCatalogEntry,
    RestBinding(
        method="PUT", path="/labware-catalog/{labware_type}", tag=RouteTag.LABWARE,
    ),
)


DELETE_LABWARE_ENTRY_ROUTE = Route(
    LabwareEntryRequest,
    NoContent,
    RestBinding(
        method="DELETE", path="/labware-catalog/{labware_type}", tag=RouteTag.LABWARE, status_code=204,
    ),
)
