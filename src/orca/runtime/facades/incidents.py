"""IncidentFacade: the external UI surface over the IncidentService.

Reads pass through. `acknowledge` / `acknowledge_all` are OPERATOR-level
because acking does not change runtime behavior -- it only marks the
incident record as seen. The underlying error condition (if still active)
is not remedied by acknowledgement.
"""

from orca.runtime.danger import DangerLevel, dangerous
from orca.runtime.incident_service import IncidentService
from orca.runtime.incident_store import (
    IncidentCategory,
    SystemIncident,
)
from orca.runtime.runtime_interface import IIncidentFacade


class IncidentFacade(IIncidentFacade):
    """Concrete IncidentFacade implementation."""

    def __init__(self, service: IncidentService) -> None:
        self._service = service

    async def list(
        self, *,
        unacknowledged_only: bool = False,
        category: IncidentCategory | None = None,
        execution_id: str | None = None,
        since: float | None = None,
    ) -> list[SystemIncident]:
        return await self._service.list(
            unacknowledged_only=unacknowledged_only,
            category=category,
            execution_id=execution_id,
            since=since,
        )

    async def get(self, incident_id: str) -> SystemIncident:
        return await self._service.get(incident_id)

    @dangerous(
        name="incidents.acknowledge",
        level=DangerLevel.OPERATOR,
        message="Acknowledge incident '{incident_id}'. This only marks the "
                "record as seen -- the underlying issue (if still active) is "
                "not remedied. Future occurrences create new incidents.",
    )
    async def acknowledge(
        self, incident_id: str,
    ) -> None:
        # Gate on a real read so an unknown id raises KeyError (-> 404):
        # the durable store's acknowledge is a fire-and-forget enqueue.
        await self._service.get(incident_id)
        self._service.acknowledge(incident_id)

    @dangerous(
        name="incidents.acknowledge_all",
        level=DangerLevel.CRITICAL,
        message="Acknowledge every unacknowledged incident (category filter: "
                "{category}). Records remain queryable; only the acknowledged "
                "flag flips.",
    )
    async def acknowledge_all(
        self, *,
        category: IncidentCategory | None = None,
    ) -> int:
        return await self._service.acknowledge_all(category=category)
