"""Deck layouts on a liquid handler. These routes need a built runtime."""

from typing import ClassVar

from pydantic import BaseModel

from orca.operations.binding import RestBinding
from orca.operations.catalog_models import NoContent
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError, message_of
from orca.operations.wire_models import DeckLayoutSummaryDTO
from orca.runtime.deck_layout_check import carrier_labware_types, deck_layout_errors
from orca.runtime.deployment_registries import IDeploymentRegistries
from orca.runtime.runtime_interface import ISystemRuntime
from orca.runtime.setup_files import SetupEntryExistsError
from orca.operations.deck_layout_models import (
    DELETE_DECK_LAYOUT_ROUTE,
    GET_DECK_LAYOUT_ROUTE,
    LIST_DECK_LAYOUTS_ROUTE,
    UPDATE_DECK_LAYOUT_ROUTE,
)
from orca.operations.deck_layout_models import (
    DeckLayoutResponse,
    DeckLayoutSummaryListResponse,
    DeleteDeckLayoutRequest,
    GetDeckLayoutRequest,
    ListDeckLayoutsRequest,
    UpdateDeckLayoutRequest,
)

def _liquid_handler_not_found(device_id: str, exc: KeyError) -> OperationError:
    return OperationError.of(
        ErrorCode.LIQUID_HANDLER_NOT_FOUND, message_of(exc), device_id=device_id,
    )


def _deck_layout_not_found(device_id: str, name: str) -> OperationError:
    return OperationError.of(
        ErrorCode.DECK_LAYOUT_NOT_FOUND,
        f"Deck layout '{name}' not found for device {device_id}", device_id=device_id,
    )


class ListDeckLayoutsOperation:
    """The deck layouts on every liquid handler, or on the one `device_id` names. Each row says which device it is on."""

    Request: ClassVar[type[BaseModel]] = LIST_DECK_LAYOUTS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = LIST_DECK_LAYOUTS_ROUTE.response
    binding: ClassVar[RestBinding] = LIST_DECK_LAYOUTS_ROUTE.binding

    def __init__(self, *, runtime: ISystemRuntime) -> None:
        self._layouts = runtime.deck_layouts

    async def run(self, req: ListDeckLayoutsRequest) -> DeckLayoutSummaryListResponse:
        if req.device_id is None:
            rows = await self._layouts.list_all()
            return DeckLayoutSummaryListResponse([
                DeckLayoutSummaryDTO(device_id=device_id, name=name, deck_type=config.deck_type)
                for device_id, name, config in rows
            ])
        try:
            named = await self._layouts.list(req.device_id)
        except KeyError as exc:
            raise _liquid_handler_not_found(req.device_id, exc) from exc
        return DeckLayoutSummaryListResponse([
            DeckLayoutSummaryDTO(device_id=req.device_id, name=name, deck_type=config.deck_type)
            for name, config in named
        ])


class GetDeckLayoutOperation:
    Request: ClassVar[type[BaseModel]] = GET_DECK_LAYOUT_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_DECK_LAYOUT_ROUTE.response
    binding: ClassVar[RestBinding] = GET_DECK_LAYOUT_ROUTE.binding

    def __init__(self, *, runtime: ISystemRuntime) -> None:
        self._layouts = runtime.deck_layouts

    async def run(self, req: GetDeckLayoutRequest) -> DeckLayoutResponse:
        try:
            config = await self._layouts.get(req.device_id, req.name)
        except KeyError as exc:
            raise _liquid_handler_not_found(req.device_id, exc) from exc
        if config is None:
            raise _deck_layout_not_found(req.device_id, req.name)
        return DeckLayoutResponse(device_id=req.device_id, name=req.name, config=config)


class UpdateDeckLayoutOperation:
    """Store a deck layout under its name, new or replacing. The one writer of deck layouts.

    The layout declares carriers only: labware reaches the deck through the engine ledger, so a layout that
    declares any resource the labware catalog does not list as a carrier is refused.
    """

    Request: ClassVar[type[BaseModel]] = UPDATE_DECK_LAYOUT_ROUTE.request
    Response: ClassVar[type[BaseModel]] = UPDATE_DECK_LAYOUT_ROUTE.response
    binding: ClassVar[RestBinding] = UPDATE_DECK_LAYOUT_ROUTE.binding

    def __init__(self, *, runtime: ISystemRuntime, registries: IDeploymentRegistries) -> None:
        self._layouts = runtime.deck_layouts
        self._registries = registries

    async def run(self, req: UpdateDeckLayoutRequest) -> DeckLayoutResponse:
        errors = deck_layout_errors(req.config, await carrier_labware_types(self._registries.labware_catalog))
        if errors:
            raise OperationError.of(
                ErrorCode.VALIDATION_ERROR, "Invalid deck layout", validation_errors=list(errors),
                hint="GET /api/labware-catalog lists the carriers and labware the layout may name",
            )
        try:
            existing = await self._layouts.get(req.device_id, req.name)
        except KeyError as exc:
            raise _liquid_handler_not_found(req.device_id, exc) from exc
        try:
            if existing is None:
                await self._layouts.add(req.device_id, req.name, req.config, reason=req.reason, confirm=True)
            else:
                await self._layouts.update(req.device_id, req.name, req.config, reason=req.reason, confirm=True)
        except (KeyError, SetupEntryExistsError) as exc:
            # The read said which; the layout came or went between the read and the write.
            raise OperationError.of(
                ErrorCode.CONFLICT, f"Deck layout '{req.name}' on {req.device_id} changed while it was being stored",
                device_id=req.device_id, name=req.name,
            ) from exc
        except ValueError as exc:
            raise OperationError.of(
                ErrorCode.VALIDATION_ERROR, str(exc), device_id=req.device_id,
            ) from exc
        return DeckLayoutResponse(device_id=req.device_id, name=req.name, config=req.config)


class DeleteDeckLayoutOperation:
    Request: ClassVar[type[BaseModel]] = DELETE_DECK_LAYOUT_ROUTE.request
    Response: ClassVar[type[BaseModel]] = DELETE_DECK_LAYOUT_ROUTE.response
    binding: ClassVar[RestBinding] = DELETE_DECK_LAYOUT_ROUTE.binding

    def __init__(self, *, runtime: ISystemRuntime) -> None:
        self._layouts = runtime.deck_layouts

    async def run(self, req: DeleteDeckLayoutRequest) -> NoContent:
        try:
            deleted = await self._layouts.delete(
                req.device_id, req.name, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise _liquid_handler_not_found(req.device_id, exc) from exc
        if not deleted:
            raise _deck_layout_not_found(req.device_id, req.name)
        return NoContent()


DECK_LAYOUT_OPERATIONS = (
    ListDeckLayoutsOperation,
    GetDeckLayoutOperation,
    DeleteDeckLayoutOperation,
)
"""The deck layout Operations built from the runtime alone. `UpdateDeckLayoutOperation` also reads the labware catalog."""
