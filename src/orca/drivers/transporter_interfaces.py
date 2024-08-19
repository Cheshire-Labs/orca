from orca.drivers.driver_interfaces import IDriver


from abc import ABC, abstractmethod
from typing import List


class ITransporterDriver(IDriver, ABC):
    """
    Interface for transporter drivers.
    Attributes:
        name (str): The name of the transporter driver.
        is_running (bool): Indicates whether the transporter is currently running.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Get the name of the transporter driver."""
        raise NotImplementedError

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """Check if the transporter is currently running."""
        raise NotImplementedError

    @abstractmethod
    async def pick(self, position_name: str, labware_type: str) -> None:
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
    async def place(self, position_name: str, labware_type: str) -> None:
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