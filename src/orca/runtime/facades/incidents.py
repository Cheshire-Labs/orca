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
from orca.runtime.recoverable_timeout import RecoverableTimeoutCoordinator
from orca.runtime.runtime_interface import IIncidentFacade


class IncidentFacade(IIncidentFacade):
    """Concrete IncidentFacade implementation."""

    def __init__(
        self, service: IncidentService,
        recoverable_timeouts: RecoverableTimeoutCoordinator,
    ) -> None:
        self._service = service
        self._recoverable_timeouts = recoverable_timeouts

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
        *, reason: str | None = None,
    ) -> None:
        del reason  # consumed by @dangerous audit
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
        reason: str | None = None,
    ) -> int:
        del reason  # consumed by @dangerous audit
        return await self._service.acknowledge_all(category=category)

    # -- Recoverable-timeout operator decisions ------------------------------

    @dangerous(
        name="recoverable_timeout.extend",
        level=DangerLevel.CRITICAL,
        message="Give the held command for incident '{incident_id}' "
                "{additional_seconds} more seconds.",
    )
    def recoverable_timeout_extend(
        self, incident_id: str, additional_seconds: float, *,
        reason: str | None = None,
    ) -> None:
        del reason  # consumed by @dangerous audit
        self._recoverable_timeouts.extend(incident_id, additional_seconds)

    @dangerous(
        name="recoverable_timeout.abort",
        level=DangerLevel.CRITICAL,
        message="Fail the held command for incident '{incident_id}'. Its "
                "failure policy fires.",
    )
    def recoverable_timeout_abort(
        self, incident_id: str, operator: str, *, reason: str,
    ) -> None:
        self._recoverable_timeouts.abort(incident_id, operator, reason)

    @dangerous(
        name="recoverable_timeout.mark_complete",
        level=DangerLevel.CRITICAL,
        message="Say the device finished the held command for incident "
                "'{incident_id}'. The engine records it as a success.",
    )
    def recoverable_timeout_mark_complete(
        self, incident_id: str, operator: str, *, reason: str,
    ) -> None:
        self._recoverable_timeouts.mark_complete(incident_id, operator, reason)
