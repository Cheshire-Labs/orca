"""The labware catalog: which labware types exist. Served with or without a runtime."""

from typing import ClassVar

from pydantic import BaseModel

from orca.operations.binding import RestBinding
from orca.operations.catalog_models import NoContent
from orca.operations.labware_catalog_models import (
    LabwareCatalogEntry,
    LabwareCatalogRequest,
    LabwareCatalogResponse,
    LabwareCatalogSummary,
    LabwareCreateRequest,
    LabwareEntryRequest,
    LabwareUpdateRequest,
)
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError
from orca.runtime.deployment_registries import IDeploymentRegistries
from orca.resource_models.labware_catalog_protocol import LabwareNotFound
from orca.runtime.labware_catalog_service import (
    LabwareCatalogConflict,
    LabwareGeometryInvalid,
    SeedLabwareReadOnly,
)
from orca.runtime.labware_catalog_store import (
    OPERATOR_CUSTOM_SOURCE,
    LabwareCatalogEntry as CatalogRow,
)
from orca.operations.labware_catalog_models import (
    ADD_LABWARE_ENTRY_ROUTE,
    DELETE_LABWARE_ENTRY_ROUTE,
    GET_LABWARE_CATALOG_ROUTE,
    GET_LABWARE_ENTRY_ROUTE,
    UPDATE_LABWARE_ENTRY_ROUTE,
)

def _not_found(labware_type: str) -> OperationError:
    return OperationError.of(
        ErrorCode.LABWARE_TYPE_NOT_FOUND, f"labware_type {labware_type!r} not in catalog",
    )


def _seed_read_only(exc: SeedLabwareReadOnly, labware_type: str) -> OperationError:
    return OperationError.of(
        ErrorCode.SEED_LABWARE_READ_ONLY, str(exc),
        labware_type=labware_type, source="plr_seed",
    )


def _geometry_invalid(exc: LabwareGeometryInvalid, labware_type: str) -> OperationError:
    return OperationError.of(
        ErrorCode.VALIDATION_ERROR, str(exc), labware_type=labware_type,
    )


class GetLabwareCatalogOperation:
    Request: ClassVar[type[BaseModel]] = GET_LABWARE_CATALOG_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_LABWARE_CATALOG_ROUTE.response
    binding: ClassVar[RestBinding] = GET_LABWARE_CATALOG_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._catalog = registries.labware_catalog

    async def run(self, req: LabwareCatalogRequest) -> LabwareCatalogResponse:
        rows = await self._catalog.list(req.category)
        return LabwareCatalogResponse(labware=[LabwareCatalogSummary.from_row(r) for r in rows])


class GetLabwareEntryOperation:
    Request: ClassVar[type[BaseModel]] = GET_LABWARE_ENTRY_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_LABWARE_ENTRY_ROUTE.response
    binding: ClassVar[RestBinding] = GET_LABWARE_ENTRY_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._catalog = registries.labware_catalog

    async def run(self, req: LabwareEntryRequest) -> LabwareCatalogEntry:
        try:
            row = await self._catalog.get(req.labware_type)
        except LabwareNotFound as exc:
            raise _not_found(req.labware_type) from exc
        return LabwareCatalogEntry.from_row(row)


class AddLabwareEntryOperation:
    """Create an operator-custom row. The source is always `operator_custom`."""

    Request: ClassVar[type[BaseModel]] = ADD_LABWARE_ENTRY_ROUTE.request
    Response: ClassVar[type[BaseModel]] = ADD_LABWARE_ENTRY_ROUTE.response
    binding: ClassVar[RestBinding] = ADD_LABWARE_ENTRY_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._catalog = registries.labware_catalog

    async def run(self, req: LabwareCreateRequest) -> LabwareCatalogEntry:
        row = CatalogRow(
            labware_type=req.labware_type,
            display_name=req.display_name,
            category=req.category,
            vendor=req.vendor,
            source=OPERATOR_CUSTOM_SOURCE,
            geometry=req.geometry,
            plr_class_name=req.plr_class_name,
        )
        try:
            created = await self._catalog.add(row)
        except LabwareCatalogConflict as exc:
            raise OperationError.of(
                ErrorCode.ALREADY_EXISTS,
                f"labware_type {req.labware_type!r} already exists in catalog", labware_type=req.labware_type,
            ) from exc
        except LabwareGeometryInvalid as exc:
            raise _geometry_invalid(exc, req.labware_type) from exc
        return LabwareCatalogEntry.from_row(created)


class UpdateLabwareEntryOperation:
    """Update an operator-custom row. Seed rows are refused."""

    Request: ClassVar[type[BaseModel]] = UPDATE_LABWARE_ENTRY_ROUTE.request
    Response: ClassVar[type[BaseModel]] = UPDATE_LABWARE_ENTRY_ROUTE.response
    binding: ClassVar[RestBinding] = UPDATE_LABWARE_ENTRY_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._catalog = registries.labware_catalog

    async def run(self, req: LabwareUpdateRequest) -> LabwareCatalogEntry:
        row = CatalogRow(
            labware_type=req.labware_type,
            display_name=req.display_name,
            category=req.category,
            vendor=req.vendor,
            source=OPERATOR_CUSTOM_SOURCE,
            geometry=req.geometry,
            plr_class_name=req.plr_class_name,
        )
        try:
            updated = await self._catalog.update(row)
        except LabwareNotFound as exc:
            raise _not_found(req.labware_type) from exc
        except SeedLabwareReadOnly as exc:
            raise _seed_read_only(exc, req.labware_type) from exc
        except LabwareGeometryInvalid as exc:
            raise _geometry_invalid(exc, req.labware_type) from exc
        return LabwareCatalogEntry.from_row(updated)


class DeleteLabwareEntryOperation:
    """Delete an operator-custom row. Seed rows are refused."""

    Request: ClassVar[type[BaseModel]] = DELETE_LABWARE_ENTRY_ROUTE.request
    Response: ClassVar[type[BaseModel]] = DELETE_LABWARE_ENTRY_ROUTE.response
    binding: ClassVar[RestBinding] = DELETE_LABWARE_ENTRY_ROUTE.binding

    def __init__(self, *, registries: IDeploymentRegistries) -> None:
        self._catalog = registries.labware_catalog

    async def run(self, req: LabwareEntryRequest) -> NoContent:
        try:
            await self._catalog.delete(req.labware_type)
        except LabwareNotFound as exc:
            raise _not_found(req.labware_type) from exc
        except SeedLabwareReadOnly as exc:
            raise _seed_read_only(exc, req.labware_type) from exc
        return NoContent()


LABWARE_CATALOG_OPERATIONS = (
    GetLabwareCatalogOperation,
    GetLabwareEntryOperation,
    AddLabwareEntryOperation,
    UpdateLabwareEntryOperation,
    DeleteLabwareEntryOperation,
)
