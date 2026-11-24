"""The one way an event reaches a server's sinks, whether a runtime sent it or not."""

import logging

from orca.events.runtime_event import RuntimeEvent
from orca.events.emitter import IEventSink, IServerEvents

logger = logging.getLogger(__name__)


class ServerEvents(IServerEvents):
    """Hands each event to every sink the server added. A runtime registers it as its one sink."""

    def __init__(self) -> None:
        self._sinks: list[IEventSink] = []

    def add_sink(self, sink: IEventSink) -> None:
        self._sinks.append(sink)

    def remove_sink(self, sink: IEventSink) -> None:
        self._sinks.remove(sink)

    def emit(self, event: RuntimeEvent) -> None:
        for sink in list(self._sinks):
            try:
                sink.on_event(event)
            except Exception:
                logger.exception("%s did not take the %s event", type(sink).__name__, event.event_name)

    def on_event(self, event: RuntimeEvent) -> None:
        self.emit(event)

