from typing import Dict
from orca.events.event_bus_interface import IEventBus
from orca.events.execution_context import ExecutionContext


class StatusManager:
    def __init__(self, event_bus: IEventBus) -> None:
        self._event_bus = event_bus
        self._status_registry: Dict[str, str] = {}

    def get_status(self, entity_id: str) -> str:
        if entity_id not in self._status_registry.keys():
            raise KeyError(f"No status found for entity {entity_id}")
        return self._status_registry[entity_id]

    def set_status(self, entity_type: str, entity_id: str, status: str, context: ExecutionContext) -> None:
        if entity_id in self._status_registry.keys() and self.get_status(entity_id) == status:
            return
        self._event_bus.emit(f"{entity_type}.{entity_id}.{status}", context)
        self._status_registry[entity_id] = status
        # print(f"Status updated for {entity_type} {entity_id}: {status}: {context}")