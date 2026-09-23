"""What a consumer is allowed to ask, split by what it actually needs.

A consumer imports the one interface it uses, so its coupling is readable in the
import line. A single facade over all four would have made every consumer depend
on the union, and nothing would stop the deadlock manager reaching tip volumes.
"""

from typing import Protocol

from orca.state.identity import LabwareRef, PositionRef
from orca.state.records import (
    InitialStateDetails,
    ObservationGapCause,
    OperationRecord,
)
from orca.state.placement import Placement, Reach
from orca.state.provenance import Provenance


class IOccupancyReader(Protocol):
    """Where things are. Read only, and the only way to ask."""

    def placement_of(self, labware: LabwareRef) -> Placement | None: ...

    def occupants_of(self, position: PositionRef) -> list[LabwareRef]: ...

    def occupant_of(self, position: PositionRef) -> LabwareRef | None: ...

    def accessible_at(self, position: PositionRef) -> list[LabwareRef]: ...


class IPlacementWriter(Protocol):
    """Moving a labware in the record.

    Held by exactly two things: `MoveAction`, the only caller of
    `mover.pick`/`mover.place`, and the placement chokepoint, which every
    operator path already routes through.
    """

    def expect(self, labware: LabwareRef, at: PositionRef) -> None: ...

    def stop_expecting(self, labware: LabwareRef) -> None: ...

    def claim(
        self, labware: LabwareRef, at: PositionRef, *, single_occupant: bool = True,
    ) -> None: ...

    def abandon_claim(self, labware: LabwareRef) -> None: ...

    def arrived(
        self, labware: LabwareRef, at: PositionRef, *,
        reach: Reach = Reach.AT_HAND, single_occupant: bool = True,
    ) -> None: ...

    def reached(self, labware: LabwareRef, reach: Reach) -> None: ...

    def retire(self, labware: LabwareRef) -> None: ...


class IContentsReader(Protocol):
    """A labware's own window onto its record.

    Narrow on purpose: the labware asks, the contents ledger decides. It speaks
    a `LabwareRef` rather than a labware, so the ledger can be tested with no
    topology and cannot form an import cycle with it.

    Reads, plus the one write a labware makes about itself: that a stretch went
    by unobserved. A gap moves no volume and no tip. It expires an attestation,
    which is why anything holding the labware may report one and why doing so
    needs no operator confirmation.
    """

    async def ops_of(self, labware: LabwareRef) -> list[OperationRecord]: ...

    def provenance_of(
        self, ops: list[OperationRecord], labware_name: str,
    ) -> Provenance: ...

    def went_unobserved(
        self, ops: list[OperationRecord], labware_name: str,
    ) -> bool: ...

    def is_behind(self, labware_name: str) -> bool: ...

    async def wire_contents(
        self, labware: LabwareRef,
    ) -> tuple[dict[str, bool] | None, dict[str, float] | None]: ...

    async def note_observation_gap(
        self, labware: LabwareRef, cause: ObservationGapCause,
    ) -> None: ...

    async def next_available_tips(
        self, labware: LabwareRef, count: int,
    ) -> list[str]: ...


class IContentsSeeder(IContentsReader, Protocol):
    """The reader plus the one write that opens a labware's record.

    Separate from `IContentsReader` because only birth calls it, and nothing
    that merely reads a labware should be able to reopen its record.
    """

    async def seed_at_birth(
        self, labware: LabwareRef, declared: InitialStateDetails | None,
    ) -> None: ...
