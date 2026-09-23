"""SystemEventBus: system-level event broadcast for RuntimeEvents.

Receives RuntimeEvents from the _SystemEventForwarder and broadcasts
to all listeners (plugins, sinks). Maintains an in-memory event log
for queries like get_events_since() and get_events_for_execution().
"""

import threading
from typing import Callable

from orca.events.runtime_event import RuntimeEvent

RuntimeEventListener = Callable[[RuntimeEvent], None]


class SystemEventBus:
    """Broadcasts RuntimeEvents to registered listeners.

    Not keyed by event name (unlike the per-workflow EventBus).
    Every listener receives every event. Listeners are synchronous
    and expected to be fast (append to list, update tracker state).

    A lock guards the listener list and event log because ``emit`` runs
    from two thread contexts: on-loop (the forwarder, on-loop Services) and
    off-loop (incident ``record`` from driver-callback executor threads).
    ``emit`` snapshots the listeners under the lock, then calls them outside
    it, so a concurrent subscribe/unsubscribe cannot corrupt the iteration.
    """

    def __init__(self) -> None:
        self._listeners: list[RuntimeEventListener] = []
        self._event_log: list[RuntimeEvent] = []
        self._lock = threading.Lock()

    def subscribe(self, listener: RuntimeEventListener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def unsubscribe(self, listener: RuntimeEventListener) -> None:
        """Remove a listener. No-op if the listener isn't registered."""
        with self._lock:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

    def emit(self, event: RuntimeEvent) -> None:
        with self._lock:
            self._event_log.append(event)
            listeners = list(self._listeners)
        for listener in listeners:
            listener(event)

    def get_events_since(self, timestamp: float | None) -> list[RuntimeEvent]:
        """Return events with ``timestamp >= timestamp``. ``None`` means no filter."""
        with self._lock:
            events = list(self._event_log)
        if timestamp is None:
            return events
        return [e for e in events if e.timestamp >= timestamp]

    def get_events_for_execution(self, execution_id: str) -> list[RuntimeEvent]:
        with self._lock:
            events = list(self._event_log)
        return [e for e in events if e.execution_id == execution_id]

    def clear_events_for_execution(self, execution_id: str) -> None:
        with self._lock:
            self._event_log = [
                e for e in self._event_log if e.execution_id != execution_id
            ]
