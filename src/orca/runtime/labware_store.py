"""Labware store implementations: InMemoryLabwareStore.

The Null* variant was removed in 2026-04 because it silently dropped every
write and made misconfigured deployments look healthy. Tests and
single-process runs now use InMemoryLabwareStore by default; production
deployments inject a real backend (a hosted deployment's DB-backed store).
"""

import time

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_identity import LabwareRelationship


class InMemoryLabwareStore:
    """Ephemeral labware store for single-session tracking."""

    def __init__(self) -> None:
        self._by_id: dict[str, LabwareInstance] = {}
        self._by_barcode: dict[str, str] = {}
        self._locations: dict[str, str] = {}
        self._location_history: dict[str, list[tuple[str, str | None, float]]] = {}
        self._relationships: list[LabwareRelationship] = []

    async def register(self, instance: LabwareInstance, execution_id: str | None = None) -> None:
        self._by_id[instance.id] = instance
        if instance.barcode is not None:
            self._by_barcode[instance.barcode] = instance.id

    async def get_by_id(self, labware_id: str) -> LabwareInstance | None:
        return self._by_id.get(labware_id)

    async def get_by_barcode(self, barcode: str) -> LabwareInstance | None:
        labware_id = self._by_barcode.get(barcode)
        if labware_id is None:
            return None
        return self._by_id.get(labware_id)

    async def update_location(self, labware_id: str, position_id: str) -> None:
        if labware_id in self._by_id:
            previous = self._locations.get(labware_id)
            self._locations[labware_id] = position_id
            self._location_history.setdefault(labware_id, []).append(
                (position_id, previous, time.time()),
            )

    async def clear_location(self, labware_id: str) -> None:
        self._locations.pop(labware_id, None)

    def get_location(self, labware_id: str) -> str | None:
        return self._locations.get(labware_id)

    async def get_by_position(self, position_id: str) -> LabwareInstance | None:
        # Most-recently-registered labware at the position wins, matching
        # DbLabwareStore's `created_at DESC`. `_by_id` preserves register order.
        for labware_id in reversed(self._by_id):
            if self._locations.get(labware_id) == position_id:
                return self._by_id[labware_id]
        return None

    async def list_active_locations(self) -> list[tuple[str, str]]:
        return list(self._locations.items())

    async def get_location_history(
        self, labware_id: str,
    ) -> list[tuple[str, str | None, float]]:
        return list(self._location_history.get(labware_id, ()))

    async def record_relationship(self, relationship: LabwareRelationship) -> None:
        self._relationships.append(relationship)

    async def get_relationships(self, labware_id: str) -> list[LabwareRelationship]:
        return [
            r for r in self._relationships
            if r.source_id == labware_id or r.target_id == labware_id
        ]

    async def delete(self, labware_id: str) -> None:
        """Remove a labware identity from the store (operator clear surfaces).

        Used by the operator clear tools when discharging /
        clearing labware that was registered through the store. Idempotent:
        unknown id is a no-op so the higher-level "clear" operation can
        sweep best-effort without choking on partially-removed state.
        """
        instance = self._by_id.pop(labware_id, None)
        if instance is not None and instance.barcode is not None:
            self._by_barcode.pop(instance.barcode, None)
        self._locations.pop(labware_id, None)
