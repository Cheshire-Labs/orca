from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import Location


import asyncio
import enum
import uuid
from typing import Callable, Optional


class ReservationPriority(enum.Enum):
    """What a reservation is, for the one case where two of them are ranked.

    Three tiers, strongest first. The top one is not a reservation, so it has
    no member here:

    1. Physical presence. Labware standing on the spot beats everything.
       ``LocationReservationManager.can_reserve`` checks occupancy after the
       ranking below, so nothing takes a spot a plate is standing on.
    2. ``MOVE_TARGET`` -- the location a thread's plate is on its way to.
    3. ``AWAITING_OPERATOR`` -- a LIVE manual-place wait holding the spot it
       asked a person to fill. The plate does not exist yet.

    ``ORDINARY`` is everything else and the default: a source held while its
    plate is carried, a device location an action needs, a corridor seat, a
    resting candidate a route may not use. None of those is a plate arriving
    at this location, so none of them ranks.

    Tier 2 beating tier 3 is the ruling from the bench, 2026-09-03: a plate
    coming home was refused a pad claimed for a plate nobody had placed, and
    each waited on the other. Tier 2 winning means the operator removes the
    returned plate before placing the next one, which is human-paced but
    always finishes. The other order finishes only if the operator happens to
    place first, and never if they are waiting to be told they can.
    """

    AWAITING_OPERATOR = "awaiting_operator"
    ORDINARY = "ordinary"
    MOVE_TARGET = "move_target"


def outranks(
    requester: ReservationPriority, holder: ReservationPriority,
) -> bool:
    """Whether ``requester`` takes a location ``holder`` has.

    One pair, named rather than ordered: a plate arriving takes a spot being
    held open for a plate nobody has placed. Everything else waits its turn,
    including one operator wait against another -- ranking those would leave
    two people holding instructions for one spot, which is the collision the
    claim exists to stop.
    """
    return (
        requester is ReservationPriority.MOVE_TARGET
        and holder is ReservationPriority.AWAITING_OPERATOR
    )


class LocationReservation:
    def __init__(
        self,
        requested_location: Location,
        labware: LabwareInstance | None = None,
        priority: ReservationPriority = ReservationPriority.ORDINARY,
    ) -> None:
        self._id = str(uuid.uuid4())
        self._labware = labware
        self._priority = priority
        self._requested_location: Location = requested_location
        self._reserved_location: Optional[Location] = None
        self._reservation_release_callback: Callable[[], None] = lambda: None
        self._displaced: bool = False
        self._released: bool = False
        self._thread_id: str | None = None
        self._membership: Optional[Callable[[str], bool]] = None
        self._pending_drain_check: Optional[Callable[[Optional[str]], bool]] = None
        self.processed: asyncio.Event = asyncio.Event()
        self.granted: asyncio.Event  = asyncio.Event()
        self.deadlocked: asyncio.Event = asyncio.Event()
        self.rejected: asyncio.Event = asyncio.Event()

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    @thread_id.setter
    def thread_id(self, value: str | None) -> None:
        self._thread_id = value

    @property
    def id(self) -> str:
        return self._id

    @property
    def labware(self) -> LabwareInstance | None:
        return self._labware

    @property
    def priority(self) -> ReservationPriority:
        """This reservation's tier; see ``ReservationPriority``."""
        return self._priority

    @property
    def membership(self) -> Optional[Callable[[str], bool]]:
        """Sanction test for a device-mutex hold: given a
        labware id, may it reserve one of the held device's sites?

        True for labware bound to the holding action's declared-input slots,
        and for labware already standing on the device, which has to be able to
        step off. None means only the holder thread enters.
        """
        return self._membership

    def set_membership(self, membership: Callable[[str], bool]) -> None:
        self._membership = membership

    @property
    def pending_drain_check(self) -> Optional[Callable[[Optional[str]], bool]]:
        """Non-None once the owning action departed with occupants still on
        owned sites. The manager evaluates it at the next
        acquisition attempt (passing the requester's labware id, which never
        blocks its own takeover) and takes the hold over when it passes --
        pull, not push, so a stale observer can never fire and a
        status-dependent predicate re-heals on the successor's poll."""
        return self._pending_drain_check

    def mark_pending_drain(self, check: Callable[[Optional[str]], bool]) -> None:
        self._pending_drain_check = check

    def set_location(self, location: Location) -> None:
        if self.rejected.is_set():
            raise ValueError("Reservation has been rejected")
        self._reserved_location = location

    @property
    def requested_location(self) -> Location:
        return self._requested_location

    @property
    def reserved_location(self) -> Location:
        if self._reserved_location is None:
            raise ValueError("Location not yet reserved")
        return self._reserved_location

    # def submit_reservation_request(self, reservation_manager: IThreadReservationCoordinator) -> None:
    #     self._reservation_manager = reservation_manager
    #     reservation_manager.submit_reservation_request(self._requested_location.name, self) 

    @property
    def is_displaced(self) -> bool:
        """True once another reservation took ownership of this location.

        ``granted`` stays set, so a displaced reservation still looks grantable
        to anything checking only that event.
        """
        return self._displaced

    def mark_displaced(self) -> None:
        self._displaced = True

    @property
    def is_released(self) -> bool:
        """True once the manager no longer holds this reservation at its position.

        Set wherever a reservation leaves the manager, including the routes a
        holder does not drive itself: an operator cancel, or the abort sweep.
        A holder that means to keep its claim for as long as it waits has to be
        able to tell that the claim is gone, and ``granted`` stays set forever.
        """
        return self._released

    def mark_released(self) -> None:
        self._released = True

    def set_reservation_release_callback(self, callback: Callable[[], None]) -> None:
        """Sets a callback to be called when the reservation is released."""
        self._reservation_release_callback = callback

    def release_reservation(self) -> None:
        self._reservation_release_callback()

    def release_and_ungrant(self) -> None:
        """Release the held lock, then drop the granted flag: the sanctioned
        order for returning a partially-granted request to the retry pool."""
        self.release_reservation()
        self.granted.clear()

    def clear(self) -> None:
        """Reset processed/deadlocked/rejected events for retry.

        Invariant: a granted reservation owns a real location lock that the
        owning thread holds. Clearing it without releasing the underlying
        lock would orphan the lock (system enters a state where the location
        appears free but the reservation_release_callback is never invoked).
        Every retry path in the reservation manager checks ``granted`` and
        only calls ``clear()`` when the request was rejected or deadlocked,
        so this guard is defensive: hitting it means a caller violated that
        invariant and we surface the misuse loudly.

        A DISPLACED reservation is exempt: it holds no lock to orphan, and a
        rejected collection can legitimately still contain one. Retrying it
        resets both flags so it competes again on equal terms.
        """
        if self.granted.is_set() and not self._displaced:
            raise RuntimeError(
                "LocationReservation.clear() invariant violated: cannot clear "
                "a granted reservation; release the underlying lock first.",
            )
        self._displaced = False
        self.granted.clear()
        self.processed.clear()
        self.deadlocked.clear()
        self.rejected.clear()