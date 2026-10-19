"""The safe point a running action body offers back to its thread."""

import asyncio
from typing import Protocol


class IPauseCheckpoint(Protocol):
    """The thread's pause machinery, as seen from inside an action body.

    ``ExecutingLabwareThread`` satisfies this structurally. An action body
    holds the device reservation and is normally the wrong place to stop, so
    only a point where nothing is in motion offers the checkpoint.
    """

    @property
    def pause_requested(self) -> asyncio.Event:
        """Set while an operator pause is pending; a wait inside the body races it."""
        ...

    async def hold_if_pause_requested(self, message: str | None = None) -> bool:
        """Latch PAUSED and wait for resume when a pause is pending, else return at once.

        True when the body carries on here: nothing was pending, or the hold
        ended in a resume into this same action. False when the body cannot
        hold at this point, or the hold ended in a stop.
        """
        ...
