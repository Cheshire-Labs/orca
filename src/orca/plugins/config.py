"""The plugins a server's config declares, and their settings.

Each server reads a JSON list of these from its environment. This module imports
no plugin, so a server's settings can import it without a cycle.
"""

from typing import Literal

from orca.events.runtime_event import RuntimeEvent
from pydantic import BaseModel, ConfigDict, Field


class EventFilter(BaseModel):
    """Selects which RuntimeEvents an integration receives.

    Both axes are AND-ed; an unset axis matches everything. Filtering by
    ``entity_types`` / ``statuses`` lets an operator forward only the events
    that matter (e.g. only ``INCIDENT`` events to a pager) without code.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_types: frozenset[str] | None = None
    statuses: frozenset[str] | None = None

    def matches(self, event: RuntimeEvent) -> bool:
        if self.entity_types is not None and event.entity_type not in self.entity_types:
            return False
        if self.statuses is not None and event.status not in self.statuses:
            return False
        return True


class WebhookPluginConfig(BaseModel):
    """Generic webhook integration: POST event JSON to a URL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["webhook"] = "webhook"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    event_filter: EventFilter | None = None
    timeout: float = 10.0
    max_retries: int = 3
    backoff_base: float = 0.5
    queue_size: int = 256


PluginConfig = WebhookPluginConfig
