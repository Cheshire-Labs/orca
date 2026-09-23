"""The one placement ledger this process reads and writes.

Position was kept twice: a `_labware` field on every holder and the location
service's own entry. Two stores that can disagree is what let a plate be at two
places at once, so there is now one.

It is reached here rather than passed in. Injection was the first design and it
is what would have kept the second store alive: `PlatePad` alone is constructed
in 173 places, and a constructor that can be called without the ledger is a
constructor that quietly makes another one. Orca runs a single system per
process, so a process-scoped ledger says exactly what is true.

`reset()` is for a system build and for the test that wants an empty world.
"""

from orca.state.placement import PlacementLedger

_ledger = PlacementLedger()


def placement_ledger() -> PlacementLedger:
    return _ledger


def reset_placement_ledger() -> None:
    """Empty the world. Called when a system is built and between tests."""
    global _ledger
    _ledger = PlacementLedger()
