from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
class IInitializableDriver(ABC):
    @property
    @abstractmethod
    def is_initialized(self) -> bool:
        """
        Check if the driver is initialized.
        Returns:
            bool: True if the driver is initialized, False otherwise.
        """
        raise NotImplementedError

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        """
        Set the initialization options for the driver.
        Args:
            init_options (Dict[str, Any]): The initialization options.
        """
        raise NotImplementedError

    @abstractmethod
    async def initialize(self) -> None:
        """
        Initialize the driver.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """
        Check if the driver is connected.
        Returns:
            bool: True if the driver is connected, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    async def connect(self) -> None:
        """
        Connect to the driver.
        """
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        """
        Disconnect from the driver.
        """
        raise NotImplementedError


class IDriver(IInitializableDriver, ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """
        Get the name of the driver.
        Returns:
            str: The name of the driver.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """
        Check if the driver is running.
        Returns:
            bool: True if the driver is running, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        """
        Execute a command with the driver.
        Args:
            command (str): The command to execute.
            options (Dict[str, Any]): The options for the command.
        """
        raise NotImplementedError

class ILabwarePlaceableDriver(IDriver, ABC):

    @abstractmethod
    async def prepare_for_pick(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        """
        Prepare the driver for picking up labware. Called before the transporter is sent to pick up labware.
        Args:
            labware_name (str): The name of the labware.
            labware_type (str): The type of the labware.
            barcode (Optional[str], optional): The barcode of the labware. Defaults to None.
            alias (Optional[str], optional): The alias of the labware. Defaults to None.
        """
        raise NotImplementedError

    @abstractmethod
    async def prepare_for_place(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        """
        Prepare the driver for placing labware.  Called before the transporter is sent to place labware.
        Args:
            labware_name (str): The name of the labware.
            labware_type (str): The type of the labware.
            barcode (Optional[str], optional): The barcode of the labware. Defaults to None.
            alias (Optional[str], optional): The alias of the labware. Defaults to None.
        """
        raise NotImplementedError

    @abstractmethod
    async def notify_picked(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        """
        Notify the driver that labware has been picked up.  Called after the transporter has picked up labware.
        Args:
            labware_name (str): The name of the labware.
            labware_type (str): The type of the labware.
            barcode (Optional[str], optional): The barcode of the labware. Defaults to None.
            alias (Optional[str], optional): The alias of the labware. Defaults to None.
        """
        raise NotImplementedError

    @abstractmethod
    async def notify_placed(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        """
        Notify the driver that labware has been placed. Called after the transporter has placed labware.
        Args:
            labware_name (str): The name of the labware.
            labware_type (str): The type of the labware.
            barcode (Optional[str], optional): The barcode of the labware. Defaults to None.
            alias (Optional[str], optional): The alias of the labware. Defaults to None.
        """
        raise NotImplementedError