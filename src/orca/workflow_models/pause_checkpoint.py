"""The safe point a running action body offers back to its thread."""

from typing import Protocol


class IPauseCheckpoint(Protocol):
    """The thread's pause machinery, as seen from inside an action body.

    ``ExecutingLabwareThread`` satisfies this structurally. An action body
    holds the device reservation and is normally the wrong place to stop, so
    only a point where nothing is in motion offers the checkpoint.
    """

    async def hold_if_pause_requested(self) -> None:
        """Latch PAUSED and wait for resume when a pause is pending, else return at once."""
        ...
