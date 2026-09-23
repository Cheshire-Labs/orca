import logging
from abc import ABC, abstractmethod
from typing import Optional

from cheshire_drivers.move_parameters import MoveParameterPatch

from orca.resource_models.device_error import MoverAlreadyHoldingError
from orca.resource_models.gripper_pad import GripperPad
from orca.resource_models.labware import LabwareInstance
from orca.resource_models.location import ILabwareLocationObserver, Location
from orca.resource_models.tracked_lock import TrackedLock
from orca.resource_models.transporter_interface import ITransporter
from orca.runtime.interfaces import IGripProfileStore

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
        self._under_external_control: bool = False
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
        return self._under_external_control or self._external_control_hold is not None

    @property
    def external_control_hold(self) -> str | None:
        """See ``Device.external_control_hold``."""
        return self._external_control_hold

    def take_external_control(self) -> None:
        self._under_external_control = True

    def release_external_control(self) -> None:
        """Per-command release; leaves an operator's standing hold alone."""
        self._under_external_control = False

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
            raise MoverAlreadyHoldingError(self._name, held.name, held.template_name)
        if location.labware is None:
            raise ValueError(f"{location} does not contain labware")
        labware = location.labware
        orca_logger.info(f"{self._name} pick {labware} from {location}: picking...")
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
        await self._do_place(location)
        # Only on success, so a failed place keeps what a retry re-issues from.
        self._picked_from = None
        orca_logger.info(f"{self._name} place {held} to {location}: placed")

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
