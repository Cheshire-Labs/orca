from typing import List, Optional
from orca.resource_models.labware import LabwareInstance


from abc import ABC, abstractmethod


class IPlateMover(ABC):
    """The acting mover in a placement hook, named here rather than in
    `transporter_interface` because that module imports `location`, which
    imports this one. Identity is all the hooks need: a device compares the
    mover against its own gripper to tell an internal hop from an external
    arm reaching in.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def labware(self) -> Optional[LabwareInstance]:
        """The plate currently in the jaws, or None."""
        raise NotImplementedError

    @property
    @abstractmethod
    def gripper_position_id(self) -> str:
        """Where a plate in these jaws is, as a position an operator can name.

        The id rather than the Location, because this module sits below
        `location` in the import order and must stay there.
        """
        raise NotImplementedError


class ILabwarePlaceable(ABC):
    @property
    def name(self) -> str:
        raise NotImplementedError

    @property
    def labware(self) -> Optional[LabwareInstance]:
        raise NotImplementedError

    @property
    def accessible_labware(self) -> Optional[LabwareInstance]:
        """What a transporter could touch at the approach point; defaults to
        the occupant (staged-load holders override to the staged plate)."""
        return self.labware

    @property
    def loaded_labware(self) -> List[LabwareInstance]:
        """Labware currently loaded at this position."""
        return []

    @property
    @abstractmethod
    def supports_deadlock_resolution(self) -> bool:
        """Returns True if this resource can be used as a parking location during deadlock resolution."""
        raise NotImplementedError

    def initialize_labware(self, labware: LabwareInstance) -> None:
        raise NotImplementedError

    @abstractmethod
    async def prepare_for_pick(self, labware: LabwareInstance, mover: IPlateMover) -> None:
        raise NotImplementedError

    @abstractmethod
    async def prepare_for_place(self, labware: LabwareInstance, mover: IPlateMover) -> None:
        raise NotImplementedError

    @abstractmethod
    async def notify_picked(self, labware: LabwareInstance, mover: IPlateMover) -> None:
        raise NotImplementedError

    @abstractmethod
    async def notify_placed(self, labware: LabwareInstance, mover: IPlateMover) -> None:
        raise NotImplementedError

    async def dispose_labware(self, labware: LabwareInstance) -> None:
        """Release a persistent reference to ``labware`` after the owning
        thread has reached its end_location. Signals that the labware has
        exited the workflow; the location becomes available for reuse.

        Default is a no-op; Device-backed gateways don't hold a persistent
        per-labware reference on arrival (the device's loaded_labware list
        is orthogonal to reservation availability, which is gated on the
        stage). Single-occupant resources (PlatePad, DeckSite, GripperPad)
        override to clear their stored ``_labware`` so subsequent threads
        can place there.
        """
        return None