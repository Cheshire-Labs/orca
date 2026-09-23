"""Off-loop-safe delivery for event sinks.

``SystemEventBus.emit`` runs from two thread contexts: on-loop (the forwarder
and on-loop Services) and off-loop (incident ``record`` fires from driver-
callback executor threads). A sink whose ``on_event`` touches loop-bound state
(an ``asyncio.Queue``, a loop future) must not do so from a foreign thread.

``deliver_on_loop`` is the shared seam: it runs ``deliver(event)`` inline when
already on ``loop``, otherwise marshals it via ``call_soon_threadsafe``. Every
sink that pushes to an ``asyncio.Queue`` (the daemon SSE sink, the runtime
event-stream subscription, a hosted deployment's WS fan-out) routes through this.
"""

import asyncio
from typing import Callable

from orca.events.runtime_event import RuntimeEvent


def deliver_on_loop(
    loop: asyncio.AbstractEventLoop | None,
    deliver: Callable[[RuntimeEvent], None],
    event: RuntimeEvent,
) -> None:
    """Run ``deliver(event)`` on ``loop``: inline if already on it, else
    thread-safely scheduled. No-op if ``loop`` is None (no loop captured yet)."""
    if loop is None:
        return
    try:
        on_loop = asyncio.get_running_loop() is loop
    except RuntimeError:
        on_loop = False
    if on_loop:
        deliver(event)
    else:
        loop.call_soon_threadsafe(deliver, event)
