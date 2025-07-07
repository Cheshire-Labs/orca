import asyncio
from orca.resource_models.location import Location
from orca.resource_models.resources import IInitializable, IResource, ISimulationable


from abc import ABC, abstractmethod
from typing import List


class ITransporter(IResource, IInitializable, ISimulationable, ABC):
    """
    Interface for transporter drivers.
    Attributes:
        name (str): The name of the transporter driver.
        is_running (bool): Indicates whether the transporter is currently running.
    """
    @property
    @abstractmethod
    def lock(self) -> asyncio.Lock:
        """
        Get the lock for the transporter.

        Returns:
            asyncio.Lock: The lock used to control access to the transporter.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def in_use(self) -> bool:
        """Check if the transporter is currently running."""
        raise NotImplementedError

    @abstractmethod
    async def pick(self, location: Location) -> None:
        """
        Pick up labware from a specified position.

        Args:
            position_name (str): The name of the position to pick from.
            labware_type (str): The type of labware being picked.

        Returns:
            None
        """
        raise NotImplementedError

    @abstractmethod
    async def place(self, location: Location) -> None:
        """
        Place labware at a specified position.

        Args:
            position_name (str): The name of the position to place at.
            labware_type (str): The type of labware being placed.

        Returns:
            None
        """
        raise NotImplementedError

    @abstractmethod
    def get_taught_positions(self) -> List[str]:
        """
        Get a list of the names of taught positions.

        Returns:
            List[str]: A list of taught positions.
        """
        raise NotImplementedError