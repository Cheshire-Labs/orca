"""LabwareService: the one way the engine reads and writes labware identity."""

import logging
import time

from orca.events.execution_context import LabwareRecordContext
from orca.events.runtime_event import RuntimeEvent
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_service_interface import ILabwareService
from orca.resource_models.store_interfaces import ILabwareStore
from orca.events.emitter import IEventEmitter
from orca.state.ops_store import SYSTEM_ID

logger = logging.getLogger(__name__)


class LabwareService:
    def __init__(self, store: ILabwareStore) -> None:
        self._store = store

    async def register(self, instance: LabwareInstance, execution_id: str | None = None) -> None:
        await self._store.register(instance, execution_id=execution_id)

    async def get_by_id(self, labware_id: str) -> LabwareInstance | None:
        return await self._store.get_by_id(labware_id)

    async def get_by_barcode(self, barcode: str) -> LabwareInstance | None:
        return await self._store.get_by_barcode(barcode)

    async def get_by_position(self, position_id: str) -> LabwareInstance | None:
        return await self._store.get_by_position(position_id)

    async def update_location(self, labware_id: str, position_id: str) -> None:
        await self._store.update_location(labware_id, position_id)

    async def clear_location(self, labware_id: str) -> None:
        await self._store.clear_location(labware_id)

    async def list_active_locations(self) -> list[tuple[str, str]]:
        return await self._store.list_active_locations()

    async def list_departed_locations(self) -> list[tuple[str, str]]:
        return await self._store.list_departed_locations()

    async def get_location_history(
        self, labware_id: str,
    ) -> list[tuple[str, str | None, float]]:
        return await self._store.get_location_history(labware_id)


class AnnouncedLabwareService(ILabwareService):
    """Sends LABWARE.<id>.<verb> after each write, so a read of the labware record knows it is stale.

    Every write to the record goes through the runtime's service, from a run's move or an operator's edit alike.
    """

    def __init__(self, service: ILabwareService, events: IEventEmitter) -> None:
        self._service = service
        self._events = events

    async def register(self, instance: LabwareInstance, execution_id: str | None = None) -> None:
        await self._service.register(instance, execution_id)
        self._announce(instance.id, "SAVED", None, execution_id)

    async def get_by_id(self, labware_id: str) -> LabwareInstance | None:
        return await self._service.get_by_id(labware_id)

    async def get_by_barcode(self, barcode: str) -> LabwareInstance | None:
        return await self._service.get_by_barcode(barcode)

    async def get_by_position(self, position_id: str) -> LabwareInstance | None:
        return await self._service.get_by_position(position_id)

    async def update_location(self, labware_id: str, position_id: str) -> None:
        await self._service.update_location(labware_id, position_id)
        self._announce(labware_id, "MOVED", position_id, None)

    async def clear_location(self, labware_id: str) -> None:
        await self._service.clear_location(labware_id)
        self._announce(labware_id, "CLEARED", None, None)

    async def list_active_locations(self) -> list[tuple[str, str]]:
        return await self._service.list_active_locations()

    async def list_departed_locations(self) -> list[tuple[str, str]]:
        return await self._service.list_departed_locations()

    async def get_location_history(
        self, labware_id: str,
    ) -> list[tuple[str, str | None, float]]:
        return await self._service.get_location_history(labware_id)

    def _announce(self, labware_id: str, verb: str, position_id: str | None, execution_id: str | None) -> None:
        # The write is already stored; an event that fails must not fail it.
        try:
            self._events.emit(RuntimeEvent(
                event_name=f"LABWARE.{labware_id}.{verb}",
                execution_id=execution_id or SYSTEM_ID,
                timestamp=time.time(),
                entity_type="LABWARE",
                entity_id=labware_id,
                status=verb,
                context=LabwareRecordContext(labware_id=labware_id, position_id=position_id),
            ))
        except Exception:
            logger.warning("could not send the %s event for labware %s", verb, labware_id, exc_info=True)
