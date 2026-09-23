"""ILabwareLocationService -- single source of truth for labware position.

Replaces the previous split between thread-owned LocationHistory and
LabwareLocationHistoryRegistry. Every position update (thread moves,
device-internal gripper moves, ctx.move()) goes through this service.
LocationHistory is maintained internally for oscillation detection.

A tracked labware is in one of three placement states. The distinction that
matters is EXPECTED vs PRESENT: an entry thread knows its labware's identity
and where that labware belongs before anything physical is there, and a ledger
that could only say "here" had to claim occupancy it did not have. Only a
PRESENT entry is a claim about physical reality, so only a PRESENT entry
reaches the durable store: a persisted positioned row IS a real physical
plate.
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List

from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location
from orca.resource_models.labware_directory import labware_directory
from orca.state.current import placement_ledger
from orca.state.identity import PositionRef
from orca.state.placement import PlacementState
from orca.workflow_models.labware_threads.location_history import LocationHistory

LocationUpdateListener = Callable[[LabwareInstance, Location], None]
LocationRetireListener = Callable[[LabwareInstance], None]
ExpectationDroppedListener = Callable[[LabwareInstance], None]


class ArrivalMechanism(str, Enum):
    """How an EXPECTED labware is going to get to where it belongs.

    Read by surfaces that tell an operator what to do, and by the register
    verb, which may only adopt an expectation a human is meant to fulfil.
    """

    MANUAL_PLACE = "MANUAL_PLACE"
    """An operator puts it down and registers it."""

    DISPENSE = "DISPENSE"
    """A stacker or hotel produces it at its output."""


@dataclass
class _Entry:
    """What the ledger does not hold: the Location object, how the labware is
    meant to arrive, and where it has been."""

    labware: LabwareInstance
    location: Location
    arrival: ArrivalMechanism | None
    history: LocationHistory = field(default_factory=LocationHistory)
    retired: bool = False
    """A lifecycle end fires its listeners once. The ledger cannot answer this
    on its own because the slot is vacated before the thread ends."""


class ILabwareLocationService(ABC):
    """Single source of truth for labware position."""

    @abstractmethod
    def expect(
        self,
        labware: LabwareInstance,
        location: Location,
        arrival: ArrivalMechanism,
    ) -> None:
        """Record that a labware belongs at a location but is not there yet.

        Appends no history (nothing has been anywhere) and fires no update
        listener, so an expectation never reaches the durable store and cannot
        be resurrected as a real plate by a reboot. ``get`` answers with the
        location, because the engine legitimately needs to know where a thread's
        labware is headed before it arrives.
        """
        ...

    @abstractmethod
    def stop_expecting(self, labware: LabwareInstance) -> None:
        """Drop an expectation that will not be fulfilled (the waiting thread
        stopped or failed before its labware arrived).

        Distinct from ``retire``: retiring records that something left the
        world and keeps its last position readable. An abandoned expectation
        never entered the world, so nothing about it is worth keeping. A
        PRESENT or RETIRED entry is left alone.
        """
        ...

    @abstractmethod
    def add_expectation_dropped_listener(
        self, listener: ExpectationDroppedListener,
    ) -> None:
        """Observe abandoned expectations, so holders that took the labware's
        identity on faith can drop it too."""
        ...

    @abstractmethod
    def update(self, labware: LabwareInstance, location: Location) -> None:
        """Record a labware position change. Appends to history.

        Marks the labware PRESENT: this is the arrival, whoever caused it.
        """
        ...

    @abstractmethod
    def add_update_listener(self, listener: LocationUpdateListener) -> None:
        """Observe position changes. Fired only when the position actually
        changes, so a listener can mirror the ledger without echoing no-ops."""
        ...

    @abstractmethod
    def retire(self, labware: LabwareInstance) -> None:
        """Record that a labware exited the world (disposed at thread end,
        operator discharge). The last-known position stays readable, because a
        lifecycle end is not a retraction of the record; retire listeners let
        mirrors clear their ACTIVE position so a reboot does not resurrect the
        labware at its end slot."""
        ...

    @abstractmethod
    def add_retire_listener(self, listener: LocationRetireListener) -> None:
        """Observe retirements. Fired once per tracked labware retired."""
        ...

    @abstractmethod
    def get(self, labware: LabwareInstance) -> Location:
        """Get the current position of a labware item. Raises KeyError if unknown.

        Answers for an EXPECTED labware too -- the location it is headed to.
        Ask ``placement`` when the difference matters.
        """
        ...

    @abstractmethod
    def placement(self, labware: LabwareInstance) -> PlacementState:
        """Whether this labware is expected, present, or retired.

        Raises KeyError if the labware is not tracked, symmetric with ``get``.
        """
        ...

    @abstractmethod
    def expected_at(
        self,
        position_id: str,
        arrival: ArrivalMechanism,
        template_name: str | None = None,
    ) -> LabwareInstance | None:
        """The longest-waiting labware expected at ``position_id``, or None.

        ``template_name`` narrows to expectations of one template; None matches
        any, which is how a caller finds out that a slot is awaited by
        something other than what it is offering.

        Longest-waiting so that two threads awaiting one slot bind to two
        different labwares in the order they started waiting, rather than both
        binding whatever showed up first.
        """
        ...

    @abstractmethod
    def get_all(self) -> dict[LabwareInstance, Location]:
        """Get all tracked labware positions. Returns a copy."""
        ...

    @abstractmethod
    def get_history(self, labware: LabwareInstance) -> LocationHistory:
        """Get the full movement history for a labware item. Raises KeyError if unknown.

        An EXPECTED labware has an empty history, not a missing one: the caller
        holds the reference and it fills in when the labware arrives.
        """
        ...

    @abstractmethod
    def reset(self, labware: LabwareInstance, location: Location) -> None:
        """Clear history and set a new initial position. For pre-start location changes."""
        ...

    @abstractmethod
    def clear_all(self) -> None:
        """Drop ALL tracked positions and histories.

        For the authoritative ``clear_all_labware`` panic button: after a
        full wipe the ledger must hold no position for any labware, so a
        stale entry can't outlive the labware it tracked.
        """
        ...


class InMemoryLabwareLocationService(ILabwareLocationService):
    """In-memory implementation. Future: DB-backed for power failure recovery."""

    def __init__(self) -> None:
        # Insertion-ordered, which is what makes `expected_at` answer
        # longest-waiting first.
        self._entries: dict[str, _Entry] = {}
        self._listeners: List[LocationUpdateListener] = []
        self._retire_listeners: List[LocationRetireListener] = []
        self._expectation_dropped_listeners: List[ExpectationDroppedListener] = []

    def add_update_listener(self, listener: LocationUpdateListener) -> None:
        self._listeners.append(listener)

    def add_retire_listener(self, listener: LocationRetireListener) -> None:
        self._retire_listeners.append(listener)

    def add_expectation_dropped_listener(
        self, listener: ExpectationDroppedListener,
    ) -> None:
        self._expectation_dropped_listeners.append(listener)

    def expect(
        self,
        labware: LabwareInstance,
        location: Location,
        arrival: ArrivalMechanism,
    ) -> None:
        existing = self._entries.get(labware.id)
        if self._state_of(labware) is PlacementState.PRESENT:
            # Already arrived; an expectation would be a step backwards.
            return
        self._entries[labware.id] = _Entry(
            labware=labware,
            location=location,
            arrival=arrival,
            history=existing.history if existing is not None else LocationHistory(),
        )
        labware_directory().remember(labware)
        placement_ledger().expect(labware.ref, PositionRef(id=location.position_id))

    def stop_expecting(self, labware: LabwareInstance) -> None:
        if self._state_of(labware) is not PlacementState.EXPECTED:
            return
        del self._entries[labware.id]
        placement_ledger().stop_expecting(labware.ref)
        for listener in self._expectation_dropped_listeners:
            listener(labware)

    def retire(self, labware: LabwareInstance) -> None:
        entry = self._entries.get(labware.id)
        if entry is None or entry.retired:
            return
        if self._state_of(labware) is PlacementState.EXPECTED:
            # Nothing ever arrived, so there is no lifecycle to end. A wipe
            # fires retire and stop_expecting together; this is the no-op half.
            return
        # Position and history stay readable (snapshots of an ENDED thread
        # report its last-known location); only the active claim is dropped.
        # Not gated on PRESENT: the holder vacates the slot first, so by the
        # time the thread ends the ledger no longer has a placement to read.
        placement_ledger().retire(labware.ref)
        entry.retired = True
        entry.arrival = None
        for listener in self._retire_listeners:
            listener(labware)

    def _notify(self, labware: LabwareInstance, location: Location) -> None:
        for listener in self._listeners:
            listener(labware, location)

    def update(self, labware: LabwareInstance, location: Location) -> None:
        entry = self._entries.get(labware.id)
        # Read the entry, not the ledger: the holder writes the ledger before
        # calling here, so a first arrival would already look PRESENT and the
        # news would never reach the durable store. An entry still carrying an
        # arrival mechanism was an expectation, and it arriving is real news.
        changed = (
            entry is None
            or entry.retired
            or entry.arrival is not None
            or entry.location.position_id != location.position_id
        )
        if entry is None:
            entry = _Entry(labware=labware, location=location, arrival=None)
            self._entries[labware.id] = entry
        else:
            entry.labware = labware
            entry.location = location
            entry.arrival = None
        entry.retired = False
        self._arrive(labware, location)
        entry.history.add_location(location, time.time())
        if changed:
            self._notify(labware, location)

    def get(self, labware: LabwareInstance) -> Location:
        entry = self._entries.get(labware.id)
        if entry is None:
            raise KeyError(f"No position tracked for labware '{labware.name}' (id={labware.id})")
        return entry.location

    def placement(self, labware: LabwareInstance) -> PlacementState:
        entry = self._entries.get(labware.id)
        if entry is None:
            raise KeyError(f"No placement tracked for labware '{labware.name}' (id={labware.id})")
        if entry.retired:
            return PlacementState.RETIRED
        state = self._state_of(labware)
        if state is None:
            raise KeyError(f"No placement tracked for labware '{labware.name}' (id={labware.id})")
        return state

    def _state_of(self, labware: LabwareInstance) -> PlacementState | None:
        held = placement_ledger().placement_of(labware.ref)
        return held.state if held is not None else None

    def _arrive(self, labware: LabwareInstance, location: Location) -> None:
        """A position may legitimately hold several (a stacker, a hotel), so
        cardinality is the holder's rule, not the ledger's, on this path."""
        labware_directory().remember(labware)
        placement_ledger().arrived(
            labware.ref, PositionRef(id=location.position_id), single_occupant=False,
        )

    def expected_at(
        self,
        position_id: str,
        arrival: ArrivalMechanism,
        template_name: str | None = None,
    ) -> LabwareInstance | None:
        for entry in self._entries.values():
            if (
                self._state_of(entry.labware) is PlacementState.EXPECTED
                and entry.arrival is arrival
                and entry.location.position_id == position_id
                and (template_name is None or entry.labware.template_name == template_name)
            ):
                return entry.labware
        return None

    def get_all(self) -> dict[LabwareInstance, Location]:
        return {entry.labware: entry.location for entry in self._entries.values()}

    def get_history(self, labware: LabwareInstance) -> LocationHistory:
        entry = self._entries.get(labware.id)
        if entry is None:
            raise KeyError(f"No history for labware '{labware.name}' (id={labware.id})")
        return entry.history

    def reset(self, labware: LabwareInstance, location: Location) -> None:
        entry = self._entries.get(labware.id)
        changed = (
            self._state_of(labware) is not PlacementState.PRESENT
            or entry is None
            or entry.location.position_id != location.position_id
        )
        if entry is None:
            entry = _Entry(labware=labware, location=location, arrival=None)
            self._entries[labware.id] = entry
        else:
            entry.labware = labware
            entry.location = location
            entry.arrival = None
            entry.history.clear()
        self._arrive(labware, location)
        entry.history.add_location(location, time.time())
        if changed:
            self._notify(labware, location)

    def clear_all(self) -> None:
        for entry in self._entries.values():
            placement_ledger().stop_expecting(entry.labware.ref)
        self._entries.clear()
