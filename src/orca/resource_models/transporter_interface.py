from orca.resource_models.location import Location
from cheshire_drivers.teachpoints import Teachpoint
from orca.resource_models.labware_placeable_interface import IPlateMover
from orca.resource_models.resources import IInitializable, IResource
from orca.resource_models.tracked_lock import TrackedLock


from abc import ABC, abstractmethod
from typing import List


class ITransporter(IResource, IInitializable, IPlateMover, ABC):
    """Interface for transporter resources.

    Implementations expose a store-backed view of teachpoints via
    `get_teachpoints` (the System graph builder's source of truth at build
    time). Path A: drivers consult the store at every dispatch, so there
    is no separate prime step on the transporter.
    """
    @property
    @abstractmethod
    def lock(self) -> TrackedLock:
        """Lock used to serialize transporter motion."""
        raise NotImplementedError

    @property
    @abstractmethod
    def in_use(self) -> bool:
        """Whether the transporter is currently holding its lock."""
        raise NotImplementedError

    @abstractmethod
    async def pick(self, location: Location) -> None:
        """Pick the labware currently at `location` into the gripper."""
        raise NotImplementedError

    @abstractmethod
    async def place(self, location: Location) -> None:
        """Place the labware in the gripper down at `location`."""
        raise NotImplementedError

    @abstractmethod
    async def get_teachpoints(self) -> List[Teachpoint]:
        """Store-backed view of this transporter's named positions.

        Awaited at topology-build time by the System graph for reachability
        computation. Drivers consult the store directly at every dispatch,
        so there is no separate driver-prime step: mid-run mutations to
        the upstream store are visible to the next move automatically.
        """
        raise NotImplementedError
