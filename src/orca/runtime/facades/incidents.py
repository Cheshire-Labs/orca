"""IncidentFacade: the external UI surface over the IncidentService.

Reads pass through. `acknowledge` / `acknowledge_all` are OPERATOR-level
because acking does not change runtime behavior -- it only marks the
incident record as seen. The underlying error condition (if still active)
is not remedied by acknowledgement.
"""

from collections.abc import Awaitable, Callable
from typing import Protocol

from orca.runtime.danger import DangerLevel, dangerous
from orca.runtime.incident_service import IIncidentService
from orca.resource_models.unheld_command import UnheldCommand
from orca.runtime.incident_store import (
    IncidentCategory,
    RecoverableTimeoutContext,
    SystemIncident,
)
from orca.runtime.recoverable_timeout import RecoverableTimeoutCoordinator, RecoverableTimeoutNotHeldError
from orca.runtime.facade_interfaces import IIncidentFacade


class IUnheldOutcomes(Protocol):
    """Settles a command whose outcome is unknown and whose incident holds nothing."""

    async def settle(self, unheld: UnheldCommand, completed: bool, filed_at: float) -> bool:
        """Returns True when completed applied nothing because the labware has moved since `filed_at`."""
        ...


class IncidentFacade(IIncidentFacade):
    """Concrete IncidentFacade implementation."""

    def __init__(
        self, service: IIncidentService,
        recoverable_timeouts: RecoverableTimeoutCoordinator,
        unheld: IUnheldOutcomes,
        clear_device_fault: Callable[[str], Awaitable[None]],
    ) -> None:
        self._service = service
        self._recoverable_timeouts = recoverable_timeouts
        self._unheld = unheld
        self._clear_device_fault = clear_device_fault

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
    async def recoverable_timeout_abort(
        self, incident_id: str, operator: str, *, reason: str,
    ) -> None:
        if await self._settle_unheld(incident_id, completed=False) is not None:
            return
        self._recoverable_timeouts.abort(incident_id, operator, reason)

    @dangerous(
        name="recoverable_timeout.mark_complete",
        level=DangerLevel.CRITICAL,
        message="Say the device finished the held command for incident "
                "'{incident_id}'. The engine records it as a success.",
    )
    async def recoverable_timeout_mark_complete(
        self, incident_id: str, operator: str, *, reason: str,
    ) -> bool:
        """Returns True when the record was left as it is because the labware has moved since the command."""
        left_as_it_is = await self._settle_unheld(incident_id, completed=True)
        if left_as_it_is is not None:
            return left_as_it_is
        self._recoverable_timeouts.mark_complete(incident_id, operator, reason)
        return False

    async def _settle_unheld(self, incident_id: str, *, completed: bool) -> bool | None:
        """A command whose outcome is unknown and that nothing holds: the decision settles its records.

        Read from the stored incident, so it settles after a restart. In the world an operator is in, as every
        operator write is. Mark complete also clears the fault the controller latched on the device, as it does for
        a held run's command. None when the incident is not one of these; otherwise whether mark complete left the
        record as it is because the labware has moved since.
        """
        try:
            incident = await self._service.get(incident_id)
        except KeyError:
            return None
        detail = incident.detail
        if not isinstance(detail, RecoverableTimeoutContext) or detail.unheld is None:
            return None
        if incident.acknowledged:
            raise RecoverableTimeoutNotHeldError(f"incident {incident_id!r} is already settled")
        left_as_it_is = await self._unheld.settle(detail.unheld, completed, incident.timestamp)
        if completed:
            await self._clear_device_fault(detail.unheld.device_name)
        self._service.acknowledge(incident_id)
        return left_as_it_is
