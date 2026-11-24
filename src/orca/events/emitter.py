from typing import Protocol

from orca.events.runtime_event import RuntimeEvent


class IEventSink(Protocol):
    """Receives RuntimeEvents from the runtime. External integration point."""

    def on_event(self, event: RuntimeEvent) -> None: ...


class IEventEmitter(Protocol):
    """Where code outside a runtime sends an event, so it reaches the same sinks a runtime's events do."""

    def emit(self, event: RuntimeEvent) -> None: ...


class IServerEvents(IEventEmitter, IEventSink, Protocol):
    """A server's one way to its sinks: code outside a runtime emits to it, and each runtime sends it every event."""

    def add_sink(self, sink: IEventSink) -> None: ...

    def remove_sink(self, sink: IEventSink) -> None: ...
