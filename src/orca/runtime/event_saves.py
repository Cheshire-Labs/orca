"""Whether every event handed to the server's event writers so far has been written.

A run is recorded as ended only after the events it fired are saved, so a reader that sees the end
also finds those events. The wait is bounded: a store that stops answering must not stop a run ending.
"""

import asyncio
from typing import Protocol

from orca.runtime.log_store_writer import LogStoreWriter

END_EVENTS_SAVE_TIMEOUT_S = 30.0
"""How long a run's end waits for its events, and a failed run's end for its threads to stop. It covers a slow store
or a slow stop, not a dead one."""


class IEventSaves(Protocol):
    async def all_saved(self, timeout: float) -> bool:
        """True once every event handed in so far is written or given up on; False if ``timeout`` ran out first.

        Events handed in after the call do not hold it up."""
        ...


class EventSaves(IEventSaves):
    """The event writers a server runs. With none, everything is already saved."""

    def __init__(self) -> None:
        self._writers: list[LogStoreWriter] = []

    def add(self, writer: LogStoreWriter) -> None:
        self._writers.append(writer)

    async def all_saved(self, timeout: float) -> bool:
        written = await asyncio.gather(*(writer.written_within(timeout) for writer in self._writers))
        return all(written)
