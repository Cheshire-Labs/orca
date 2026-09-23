"""Whether a command takes the device, or rides alongside it.

Five things about dispatch turn on this, and they all follow from one fact:
a world-sync op does not occupy the instrument. It is a bookkeeping push to a
driver's own model of the deck, not a motion, so it must not queue behind a
workflow command, must not block one, must not be refused while the device is
faulted, and cannot leave anything mid-motion to fault it for.
"""

from enum import Enum


class CommandKind(Enum):
    """What a command does to the device it is sent to."""

    ACTUATION = "actuation"
    """It takes the device. One at a time, it is refused while the device is
    faulted, it is bounded by a timer, and a failure part-way through it can
    leave the instrument somewhere nobody chose."""

    WORLD_SYNC = "world_sync"
    """It pushes state into a driver's own model and touches no hardware.

    ``seed_position``, ``ensure_seeded``, ``unseed_position`` and
    ``reset_world``, fired by the transporter's observer fan-out. Idempotent at the driver and best-effort:
    missing one is recoverable at the next event boundary, and blocking a
    workflow command for one would be the worse trade.
    """

    @property
    def occupies_the_device(self) -> bool:
        """Whether this command holds the device for its duration."""
        return self is CommandKind.ACTUATION
