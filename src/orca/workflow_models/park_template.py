"""ParkTemplate: yield primitive for suspending a thread at a parking location."""

from collections.abc import AsyncIterator
from enum import Enum

from orca.events.event_channel import EventChannelRegistry
from orca.system.reservation_manager.errors import MoveAbandonedError
from orca.workflow_models.labware_threads.i_thread_context import IThreadContext
from orca.workflow_models.labware_threads.thread_state_machine import ThreadEvent
from orca.workflow_models.method import ExecutingMethod
from orca.workflow_models.method_template import IMethodTemplate


class WakeReason(str, Enum):
    """Why a parked thread was woken."""
    ASSIGNMENT = "assignment"
    SHUTDOWN = "shutdown"
    ABORT = "abort"


class ParkTemplate(IMethodTemplate):
    """Declares that this thread should park at a location and wait for the next assignment.

    The schedule() body marks the labware PARKED and moves it toward the park
    location. A park COMPLETES only at an author-declared spot: the engine
    never relocates a park (owner decision, 2026-08-10). Several candidate
    spots may be declared (a hotel's pads); the park completes at whichever
    is granted, chosen by route score rather than declaration order. The one
    early exit is work arriving in the thread's slot, which makes the trip
    moot (the labware serves from where it rests). Park yields no method.
    """

    def __init__(self, location: str | list[str]) -> None:
        self._locations = [location] if isinstance(location, str) else list(location)
        if not self._locations:
            raise ValueError("orca.park needs at least one location")

    @property
    def name(self) -> str:
        return f"park:{'|'.join(self._locations)}"

    @property
    def location(self) -> str:
        return self._locations[0]

    @property
    def locations(self) -> list[str]:
        return list(self._locations)

    async def schedule(
        self,
        ctx: IThreadContext,
        registry: EventChannelRegistry,
    ) -> AsyncIterator[ExecutingMethod]:
        ctx.release_holdover()

        # Queued work makes the park moot, at entry AND while the move is
        # unresolved: a dispatch needing this thread must not deadlock on its park.
        slot = ctx.my_slot()

        def _work_arrived() -> bool:
            return slot is not None and not slot.queue_empty()

        if _work_arrived():
            return

        ctx.mark_my_labware_parked()
        park_locations = [ctx.location(name) for name in self._locations]
        while ctx.current_location not in park_locations:
            if _work_arrived():
                return
            try:
                await ctx.fire_and_execute_move_to(
                    ThreadEvent.PARK_MOVE_REQUESTED, park_locations,
                    abandon_when=_work_arrived,
                )
            except MoveAbandonedError:
                return
        # Park drives a move; never produces a method. The unreachable
        # ``yield`` marks this as an async generator (matches schedule's
        # ``AsyncIterator[ExecutingMethod]`` return) without dead code
        # after the function body's logical end.
        if False:
            yield
