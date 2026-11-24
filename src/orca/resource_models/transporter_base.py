import logging
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Optional

from cheshire_drivers.move_parameters import MoveParameterPatch

from orca.resource_models.deck_access import device_behind
from orca.resource_models.device_error import MoverAlreadyHoldingError
from orca.resource_models.gripper_pad import GripperPad
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import ILabwareLocationObserver, Location
from orca.resource_models.reach_in import reaching_in
from orca.resource_models.tracked_lock import TrackedLock
from orca.resource_models.transporter_interface import ITransporter
from orca.resource_models.store_interfaces import IGripProfileStore

orca_logger = logging.getLogger("orca")


class TransporterBase(ITransporter, ILabwareLocationObserver, ABC):
    """Actuator-agnostic plate-mover mechanics.

    Owns the held-labware guards, lock/in-use gating, and pick/place state
    ordering; subclasses supply only actuation via `_do_pick`/`_do_place`.
    Deliberately no driver, no teachpoint store, no auto-home - those belong
    to the concrete external `Transporter`.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._lock = TrackedLock(f"{name} transporter lock")
        self._external_control_scopes = 0
        self._external_control_hold: str | None = None
        self._picked_from: str | None = None
        # Never a map node: not a place labware rests, and staying off the
        # graph keeps observer wire ops out of the mover lock on every pick.
        self._gripper_location = Location(
            f"{name}/gripper", GripperPad(f"{name}/gripper")
        )
        self._grip_profile_store: Optional[IGripProfileStore] = None

    def bind_grip_profiles(self, store: IGripProfileStore) -> None:
        """Inject the per-labware-type grip profiles.

        On the base rather than the external arm because how a labware is held
        is a fact about the labware, not about which gripper is holding it. A
        deck gripper needs the same answer an arm does.
        """
        self._grip_profile_store = store

    async def grip_profile(
        self, labware_type: str | None,
    ) -> Optional[MoveParameterPatch]:
        """What the type being carried says about how it is held, if anything."""
        if labware_type is None or self._grip_profile_store is None:
            return None
        return await self._grip_profile_store.get(labware_type)

    @property
    def gripper_location(self) -> Location:
        """The in-flight holding position for labware this mover carries."""
        return self._gripper_location

    @property
    def pick_moves_the_plate(self) -> bool:
        """True when a returned ``pick`` means the plate is off its slot.

        False for a mover whose driver carries the plate from source to target
        in ONE call: its pick actuates nothing and only notes where the plate is
        coming from, so a refused place leaves the plate exactly where it was.
        Whoever records the move reads this to know which of the two a failure
        left behind.
        """
        return True

    @property
    def picked_from_position_id(self) -> str | None:
        """Where the plate in these jaws was picked from, or None when the jaws
        are empty. Survives a failed place so a retry re-issues the same move,
        and so a deck projection can name the site the plate left."""
        return self._picked_from

    @property
    def gripper_position_id(self) -> str:
        return self._gripper_location.position_id

    @property
    def name(self) -> str:
        return self._name

    @property
    def lock(self) -> TrackedLock:
        return self._lock

    @property
    def in_use(self) -> bool:
        """Mirror of ``Device.in_use``: lock-held OR gateway-held. Reads the
        property, not the field, so a subclass that derives external control
        from a paired device keeps the two in agreement."""
        return self._lock.locked() or self.under_external_control

    @property
    def under_external_control(self) -> bool:
        """See ``Device.under_external_control`` for the contract."""
        return self._external_control_scopes > 0 or self._external_control_hold is not None

    @property
    def external_control_hold(self) -> str | None:
        """See ``Device.external_control_hold``."""
        return self._external_control_hold

    def take_external_control(self) -> None:
        """See ``Device.take_external_control``."""
        self._external_control_scopes += 1

    def release_external_control(self) -> None:
        """See ``Device.release_external_control``."""
        self._external_control_scopes = max(0, self._external_control_scopes - 1)

    def hold_external_control(self, reason: str | None = None) -> None:
        self._external_control_hold = reason or ""

    def release_external_control_hold(self) -> None:
        self._external_control_hold = None

    @property
    def labware(self) -> Optional[LabwareInstance]:
        """What this mover is holding, read off the gripper position itself so
        there is only ever one answer to keep true."""
        return self._gripper_location.labware

    async def pick(self, location: Location) -> None:
        held = self.labware
        if held is not None:
            # Before actuating: a guard against closing on a second plate is
            # worth nothing once the arm has moved.
            raise MoverAlreadyHoldingError(self._name, held.id, held.name, held.template_name)
        if location.labware is None:
            raise ValueError(f"{location} does not contain labware")
        labware = location.labware
        orca_logger.info(f"{self._name} pick {labware} from {location}: picking...")
        async with self.reach_into(location, "pick"):
            await self._do_pick(location)
        self._picked_from = location.position_id
        orca_logger.info(f"{self._name} pick {labware} from {location}: picked")

    async def place(self, location: Location) -> None:
        held = self.labware
        if held is None:
            raise ValueError(f"{self} does not contain labware")
        if location.labware is not None:
            raise ValueError(f"{location} already contains labware")
        orca_logger.info(f"{self._name} place {held} to {location}: placing...")
        async with self.reach_into(location, "place"):
            await self._do_place(location)
        # Only once it ran, so a failed place keeps what a retry re-issues from.
        self._picked_from = None
        orca_logger.info(f"{self._name} place {held} to {location}: placed")

    async def pick_with_nothing_on_record(self, location: Location) -> None:
        """An operator's pick where the record has no plate: the device's default grip, and the jaws stay empty."""
        held = self.labware
        if held is not None:
            raise MoverAlreadyHoldingError(self._name, held.id, held.name, held.template_name)
        async with self.reach_into(location, "pick"):
            await self._do_pick(location)
        self._picked_from = location.position_id

    async def place_with_nothing_on_record(self, location: Location) -> None:
        """An operator's place with nothing in the jaws on record: the device's default grip."""
        if location.labware is not None:
            raise ValueError(f"{location} already contains labware")
        async with self.reach_into(location, "place"):
            await self._do_place(location)
        self._picked_from = None

    @asynccontextmanager
    async def reach_into(self, site: Location, purpose: str) -> AsyncIterator[None]:
        """The reach-in lease: hold the device behind ``site`` while this arm is inside it.

        The only way to a driver's pick and place. Take the mover's own lock
        first, and run the site's hooks outside, since each takes the
        device lock.
        """
        device = device_behind(site)
        with reaching_in(self._name):
            if device is None or device.lock.held_by_this_task:
                yield
                return
            async with device.reach_in_lease(self, purpose):
                yield

    @asynccontextmanager
    async def holding_the_site_through_its_records(self, site: Location, purpose: str) -> AsyncIterator[None]:
        """The reach-in lease, held from before the pick until its records are written.

        Released between them, another command queued on the device could run first, with the plate in the jaws
        and the record still showing it on the site.
        """
        async with self.reach_into(site, purpose):
            yield

    async def make_room_at(self, site: Location) -> None:
        """Wait until the device behind ``site`` can take this arm, then let it go."""
        async with self.reach_into(site, "make room"):
            return

    async def reset_labware_state(self) -> None:
        """Drop this mover's held-labware identity. Satisfies
        `ILabwareStateHolder` so the clear-all panic button can free a mover
        left holding a plate by an abandoned move; without it such a mover
        rejects every later pick."""
        self._picked_from = None
        held = self.labware
        if held is not None:
            await self._gripper_location.dispose_labware(held)

    async def reset_labware_state_everywhere(self) -> None:
        """Reset every world this mover projects into. Driven by clear-all.

        Default: the held identity is one fact whatever world is dispatching,
        so this is the same call. A mover with a per-world graph overrides.
        """
        await self.reset_labware_state()

    async def refuse_unless_taught(self, position_id: str) -> None:
        """Raise TeachpointNotFoundError when this mover reaches sites by teachpoint and has none for `position_id`."""
        return None

    @abstractmethod
    async def _do_pick(self, location: Location) -> None:
        """Actuate the physical pick; base handles guards and held state."""
        raise NotImplementedError

    @abstractmethod
    async def _do_place(self, location: Location) -> None:
        """Actuate the physical place; base handles guards and held state."""
        raise NotImplementedError

    def __str__(self) -> str:
        return f"{type(self).__name__}: {self._name}"
