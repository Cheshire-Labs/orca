"""LabwarePlacer: the single chokepoint that records a plate at a location.

Every path that asserts a plate at a location (operator place, thread-start-on-
deck, reuse-bind, boot-rehydrate) routes through ``place`` so all the position
holders downstream consumers read move together: the location slot + the
staging-bridge loaded-list (``Location.place_labware``), the position ledger,
and the LH driver deck projection (this one plate, never the whole deck).

The transporter world graph is deliberately NOT written here: a deck resident
lives on a child site, not the routing node (the device handoff), which must
stay free for transit. The bridge presents the plate at the handoff transiently
when it is picked, so the routing graph needs no placement-time write.

Colocated with the location service on purpose: when the store becomes the
single source of truth for position, this is the one unit that
folds into the DB-backed location service's ``place``.
"""

from typing import Protocol

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.labware_location_service import ILabwareLocationService
from orca.resource_models.location import Location


class ProjectLabware(Protocol):
    """Make each distinct liquid-handler deck these locations sit on agree about
    this ONE labware, leaving everything else on those decks alone."""

    async def __call__(
        self, labware: LabwareInstance, *locations: Location,
    ) -> None: ...


class LabwarePlacer:
    def __init__(
        self,
        location_service: ILabwareLocationService,
        project_labware: ProjectLabware,
    ) -> None:
        self._location_service = location_service
        self._project_labware = project_labware

    async def place(self, labware: LabwareInstance, target: Location) -> None:
        """Record ``labware`` at ``target`` across every position holder.

        Ordered so a placement lands whole or not at all. Two steps can refuse:
        the slot write (``SlotOccupiedError`` on a busy slot) and the device
        projection, which reaches a live instrument and fails when one cannot be
        reached. The slot runs first because the projection reads it, and is
        released again if the projection then fails. The position ledger is
        written last, once nothing can still fail: its listeners persist the
        placement and mirror it outward, and those are the writes with no undo.
        """
        await target.place_labware(labware)
        try:
            await self.project_devices(labware, target)
        except Exception:
            await target.dispose_labware(labware)
            raise
        self._location_service.update(labware, target)

    async def picked_up(self, labware: LabwareInstance, jaws: Location) -> None:
        """The labware is in the jaws. Called the instant the pick returns.

        Before the source is told, because that is a device call that can fail
        and the plate is already off the source when it does.
        """
        await jaws.place_labware(labware)
        self._location_service.update(labware, jaws)

    async def set_down(
        self, labware: LabwareInstance, at: Location, jaws: Location,
    ) -> None:
        """The labware is out of the jaws and at ``at``."""
        await jaws.dispose_labware(labware)
        self._location_service.update(labware, at)

    async def put_back(
        self, labware: LabwareInstance, source: Location, jaws: Location,
    ) -> None:
        """The transfer never happened: the labware is back on ``source``.

        For a mover whose driver carries the plate in one call, a refused place
        means nothing ever left the slot. The slot is written before the jaws
        are cleared so the labware is never at no place at all, and the ledger
        last, matching ``place``. No device projection: the driver never lost
        the plate, so it has nothing to be told.
        """
        await source.place_labware(labware)
        await jaws.dispose_labware(labware)
        self._location_service.update(labware, source)

    async def relocate(
        self, labware: LabwareInstance, source: Location, target: Location,
    ) -> None:
        """Move ``labware`` from one location to another, whole or not at all.

        Both holders move before anything is projected. The projection reads the
        slots, so projecting a target while the source still holds the same
        labware shows it at two sites at once -- an ordinary operator move
        between two slots of one liquid handler. When the projection fails for
        its own reasons, both holders go back to where they were.

        Source and target are both handed to the projection: one deck sees the
        labware arrive at its new site, and a deck it left sees it go.
        """
        await target.place_labware(labware)
        try:
            await source.dispose_labware(labware)
            await self._project_labware(labware, target, source)
        except Exception:
            await target.dispose_labware(labware)
            await source.place_labware(labware)
            raise
        self._location_service.update(labware, target)

    async def project_devices(self, labware: LabwareInstance, target: Location) -> None:
        """Make the LH driver deck agree about this plate (no-op off-LH).

        Used by the spawn path, where the slot + bridge loaded-list are
        written by ``place_labware`` and the position ledger by the thread
        constructor, leaving only the deck projection. Also used after a
        dispose, where no slot holds the plate any more and the projection
        takes it off the driver instead.

        Only this plate is projected. The deck-wide rebuild belongs to the
        reconcile, which a single arrival has no reason to trigger.

        The transporter graph is deliberately NOT seeded here: a deck resident
        lives on a child site, not the routing node (the device handoff), which
        must stay free for transit labware. The transporter learns of a plate
        at the handoff only transiently, when the bridge presents it for a pick.
        """
        await self._project_labware(labware, target)

    async def bind_resident(self, labware: LabwareInstance, target: Location) -> None:
        """Finish placing a resident whose slot is already claimed in-lock by
        reuse-bind. Projects onto the LH deck. Does NOT write the slot (already
        claimed in-lock; for a bare device endpoint, re-writing would re-stage
        and raise) nor the position ledger (the reuse-bind thread's constructor
        writes it; a second write duplicates history).

        It does announce the arrival. The in-lock claim writes the slot and
        tells nobody, so an action counting its inputs never heard about a
        resident that bound after it started waiting.
        """
        await self.project_devices(labware, target)
        await target.notify_initialized(labware)
