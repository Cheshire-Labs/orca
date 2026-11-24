"""Wait for the next event that needs a person, read from the stored events."""

import asyncio
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from orca.runtime.intervention import InterventionKind, classify_intervention
from orca.operations.binding import RestBinding
from orca.operations.events import IEventLog, ListEventsRequest, PastEvent
from orca.operations.route_tags import RouteTag
from orca.operations.binding import RestBinding, Route

DEFAULT_TIMEOUT_SECONDS = 25.0
MAX_TIMEOUT_SECONDS = 600.0
POLL_INTERVAL_SECONDS = 0.25
_PAGE_SIZE = 500


class Intervention(BaseModel):
    """One intervention: what to tell the operator and what to confirm."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    seq: int
    event_name: str
    execution_id: str | None = None
    thread_id: str | None = None
    labware_id: str | None = None
    labware_name: str | None = None
    labware_template_name: str | None = None
    target_location: str | None = None
    instruction: str | None = None
    step_id: str | None = None
    incident_id: str | None = None
    category: str | None = None
    severity: str | None = None
    message: str | None = None
    recovery_action: str | None = None


class NextInterventionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    after_seq: int | None = Field(
        default=None, ge=0,
        description="The `next_cursor` of the last call. Omit to wait only for interventions from now.",
    )
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0, le=MAX_TIMEOUT_SECONDS)


class NextInterventionResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    intervention: Intervention | None
    next_cursor: int


def _text(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


def _intervention(event: PastEvent, kind: InterventionKind) -> Intervention:
    context = event.context
    return Intervention(
        kind=kind.value,
        seq=event.cursor,
        event_name=event.event_name,
        execution_id=event.execution_id or None,
        thread_id=_text(context.get("thread_id")),
        labware_id=_text(context.get("labware_id")),
        labware_name=_text(context.get("labware_name")),
        labware_template_name=_text(context.get("labware_template_name")),
        target_location=_text(context.get("target_location")),
        instruction=_text(context.get("instruction")),
        step_id=_text(context.get("step_id")),
        incident_id=_text(context.get("incident_id")),
        category=_text(context.get("category")),
        severity=_text(context.get("severity")),
        message=_text(context.get("message")),
        recovery_action=_text(context.get("recovery_action")),
    )


NEXT_INTERVENTION_ROUTE = Route(
    NextInterventionRequest,
    NextInterventionResponse,
    RestBinding(
        method="GET", path="/interventions/next", tag=RouteTag.INTERVENTIONS, long_poll=True,
    ),
)


class NextInterventionOperation:
    Request: ClassVar[type[BaseModel]] = NEXT_INTERVENTION_ROUTE.request
    Response: ClassVar[type[BaseModel]] = NEXT_INTERVENTION_ROUTE.response
    binding: ClassVar[RestBinding] = NEXT_INTERVENTION_ROUTE.binding

    def __init__(self, event_log: IEventLog, poll_interval: float = POLL_INTERVAL_SECONDS) -> None:
        self._event_log = event_log
        self._poll_interval = poll_interval

    async def run(self, req: NextInterventionRequest) -> NextInterventionResponse:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + req.timeout_seconds
        scanned = req.after_seq if req.after_seq is not None else await self._latest_cursor()
        while True:
            page = await self._event_log.events(ListEventsRequest(since=scanned, limit=_PAGE_SIZE))
            for event in page:
                kind = classify_intervention(event)
                if kind is not None:
                    return NextInterventionResponse(
                        intervention=_intervention(event, kind), next_cursor=event.cursor,
                    )
            if page:
                scanned = page[-1].cursor
            remaining = deadline - loop.time()
            if remaining <= 0:
                # One writer stores events in cursor order, so nothing later can land at or below `scanned`.
                return NextInterventionResponse(intervention=None, next_cursor=scanned)
            if len(page) < _PAGE_SIZE:
                await asyncio.sleep(min(self._poll_interval, remaining))

    async def _latest_cursor(self) -> int:
        newest = await self._event_log.events(ListEventsRequest(tail=True, limit=1))
        return newest[-1].cursor if newest else 0
