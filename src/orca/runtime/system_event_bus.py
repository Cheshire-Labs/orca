"""SystemEventBus: system-level event broadcast for RuntimeEvents.

Receives RuntimeEvents from the _SystemEventForwarder and broadcasts
to all listeners (plugins, sinks). It keeps no events; the server's sinks store them.
"""

import logging
import threading
from typing import Callable

from orca.events.runtime_event import RuntimeEvent

RuntimeEventListener = Callable[[RuntimeEvent], None]

logger = logging.getLogger(__name__)


class SystemEventBus:
    """Broadcasts RuntimeEvents to registered listeners.

    Not keyed by event name (unlike the per-workflow EventBus).
    Every listener receives every event. Listeners are synchronous
    and expected to be fast (append to list, update tracker state).

    Listeners are watchers only. A listener that raises is logged and the
    others still run; the emitter never sees the error. A record that must
    stay correct is written where its change happens, never from here.

    A lock guards the listener list because ``emit`` runs
    from two thread contexts: on-loop (the forwarder, on-loop Services) and
    off-loop (incident ``record`` from driver-callback executor threads).
    ``emit`` snapshots the listeners under the lock, then calls them outside
    it, so a concurrent subscribe/unsubscribe cannot corrupt the iteration.
    """

    def __init__(self) -> None:
        self._listeners: list[RuntimeEventListener] = []
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
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                logger.exception("event listener %r failed on %s", listener, event.event_name)
