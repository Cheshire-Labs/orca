"""``SharedMethodCoordination``: owns the **method-scoped** owner-vs-contributor
protocol for ``ExecutingMethod``. Lock + events + contributor set.

Scope boundary: this is method-level only. The action-scoped participant group
(pause / recovery decision / outcome for the threads converged on ONE action) lives
in ``SharedActionCoordination``; the two are siblings under ``ExecutingMethod`` and
neither references the other. Do not add action-scoped state here.

``ExecutingMethod`` constructs a ``SharedMethodCoordination`` instance and
exposes it via the ``shared_coord`` property. Threads that join a shared
method via ``orca.join()`` register as contributors through
``add_contributor(thread_id)`` (or the ``contributor(thread_id)`` async
context manager); the owner thread queries ``is_contributor(thread_id)`` to
suppress
auto-spawn and route to ``wait_for_current_action`` instead of
``resolve_current_action``.

Replaces the scattered ``ExecutingLabwareThread._executing_shared_method``
flag pattern.
"""
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Set


class SharedMethodCoordination:
    """Owner-vs-contributor coordination for one ``ExecutingMethod``.

    State carried:

    - ``resolving_action_lock``: serializes access to the method's
      ``_current_action`` binding. The owner holds it across
      ``resolve_current_action``; contributors observe the binding via
      ``current_action_resolved`` rather than racing for the lock.
    - ``current_action_resolved``: set after the owner binds
      ``_current_action``; cleared between actions. Contributors park
      on this event.
    - ``exit_signal``: set on skip or abort to interrupt co-labware
      waits.
    - ``completed``: terminal-lifecycle signal -- set when the method
      reaches a terminal status.
    - ``_contributors``: thread ids currently registered as contributors
      via ``add_contributor`` / ``contributor``.
    """

    def __init__(self) -> None:
        self.resolving_action_lock: asyncio.Lock = asyncio.Lock()
        self.current_action_resolved: asyncio.Event = asyncio.Event()
        self.exit_signal: asyncio.Event = asyncio.Event()
        self.completed: asyncio.Event = asyncio.Event()
        self._contributors: Set[str] = set()

    @property
    def is_shared(self) -> bool:
        """True once at least one thread has joined as a contributor, i.e.
        more than one participant shares this method's current action."""
        return bool(self._contributors)

    def add_contributor(self, thread_id: str) -> None:
        self._contributors.add(thread_id)

    def remove_contributor(self, thread_id: str) -> None:
        self._contributors.discard(thread_id)

    def is_contributor(self, thread_id: str) -> bool:
        return thread_id in self._contributors

    @asynccontextmanager
    async def contributor(self, thread_id: str) -> AsyncIterator[None]:
        """Async context manager that registers ``thread_id`` as a
        contributor for the lifetime of the ``async with`` block and
        cleans up via ``finally`` so a raised exception still
        unregisters.
        """
        self.add_contributor(thread_id)
        try:
            yield
        finally:
            self.remove_contributor(thread_id)
