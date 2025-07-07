from abc import ABC, abstractmethod
import asyncio
from typing import Any, Dict

import logging

from orca.resource_models.labware_placeable_interface import ILabwarePlaceable

orca_logger = logging.getLogger("orca")
    
class IResource(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError


class IInitializable(ABC):
    @property
    @abstractmethod
    def is_initialized(self) -> bool:
        """
        Check if the driver is initialized.
        Returns:
            bool: True if the driver is initialized, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    async def initialize(self) -> None:
        """
        Initialize the driver.
        """
        raise NotImplementedError
    

# class IEquipment(IResource, IInitializable, ABC):
    
#     @property
#     @abstractmethod
#     def is_running(self) -> bool:
#         raise NotImplementedError


    # @abstractmethod
    # async def execute(self, command: str, options: Dict[str, Any]) -> None:
    #     raise NotImplementedError
    
class IConnectable(ABC):
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
    
# class Equipment(IEquipment):

#     def __init__(self, name: str) -> None:
#         self._name = name

#     @property
#     def name(self) -> str:
#         return self._name
    
#     @property
#     def is_initialized(self) -> bool:
#         return self._driver.is_initialized
    
#     @property
#     def is_running(self) -> bool:
#         return self._driver.is_running
    
#     async def initialize(self) -> None:
#         orca_logger.info(f"Initializing...")
#         orca_logger.info(f"Name: {self._name}")
#         await self._driver.initialize()
#         orca_logger.info(f"Initialized")

    # async def execute(self, command: str, options: Dict[str, Any]) -> None:
    #     if command is None:
    #         raise ValueError(f"{self} - No command to execute")
    #     orca_logger.info(f"{self} - execute - {command}")
    #     orca_logger.info(f"{self} - {command} executing...")
    #     await self._driver.execute(command, options)
    #     orca_logger.info(f"{self} - {command} executed")

    # @property
    # def is_connected(self) -> bool:
    #     return self._driver.is_connected

    # async def connect(self) -> None:
    #     orca_logger.info(f"{self} - Connecting...")
    #     await self._driver.connect()
    #     orca_logger.info(f"{self} - Connected")

    # async def disconnect(self) -> None:
    #     orca_logger.info(f"{self} - Disconnecting...")
    #     await self._driver.disconnect()
    #     orca_logger.info(f"{self} - Disconnected")



class ISimulationable(ABC):
    @property
    @abstractmethod
    def is_simulating(self) -> bool:
        """Returns whether the resource is simulating or not."""
        raise NotImplementedError

    @abstractmethod
    def set_simulating(self, sim: bool) -> None:
        """Sets the simulation state of the resource."""
        raise NotImplementedError


class IDevice(IResource, IInitializable, ILabwarePlaceable, ISimulationable, ABC):
    @property
    def lock(self) -> asyncio.Lock:
        """
        Get the lock for the device.
        
        Returns:
            asyncio.Lock: The lock used to control access to the device.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def in_use(self) -> bool:
        """ Check if the device is running."""
        raise NotImplementedError


# class IDriver(IInitializable, ABC):
#     @property
#     @abstractmethod
#     def name(self) -> str:
#         """
#         Get the name of the driver.
#         Returns:
#             str: The name of the driver.
#         """
#         raise NotImplementedError

#     @property
#     @abstractmethod
#     def is_running(self) -> bool:
#         """
#         Check if the driver is running.
#         Returns:
#             bool: True if the driver is running, False otherwise.
#         """
#         raise NotImplementedError

    # @abstractmethod
    # async def execute(self, command: str, options: Dict[str, Any]) -> None:
    #     """
    #     Execute a command with the driver.
    #     Args:
    #         command (str): The command to execute.
    #         options (Dict[str, Any]): The options for the command.
    #     """
    #     raise NotImplementedError

 

    