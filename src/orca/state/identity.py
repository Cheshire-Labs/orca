"""How this module names the things it answers about.

A ledger takes an identity, never a caller's object. That is what keeps it
testable with no topology and unable to form an import cycle with `Location`.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LabwareRef:
    """A labware's id and its name, together, because the record needs both.

    The id is the identity. The name is how a record spells it: operation
    records are written by observers that see a driver's resource names, and
    the durable store indexes on the name. A record written before ids existed
    carries only a name and can be attributed no other way.

    Two fields rather than one is a deliberate choice. The alternative was to
    make every record carry the id and fold on
    id alone, which would silently drop every record already on disk -- and a
    dropped tip record is exactly the bug this module exists to stop.

    Names are not unique over time: a PLR-backed resident reuses a fixed name
    every boot and retire keeps its ops. So the id narrows the name wherever a
    record carries one, and the pair is never split.
    """

    id: str
    name: str


@dataclass(frozen=True)
class PositionRef:
    """A place a labware can be, named the way the topology names it.

    A bare id, so the module can answer "what is at pad1" without importing the
    thing pad1 is.
    """

    id: str
