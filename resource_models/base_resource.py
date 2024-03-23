from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from resource_models.labware import Labware

class ResourceUnavailableError(Exception):
    def __init__(self, message: str = "Resource is unavailable.") -> None:
        super().__init__(message)
    
    
class IResource(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError
    

class IInitializableResource(IResource, ABC):
    
    @property
    def is_initialized(self) -> bool:
        raise NotImplementedError
    
    @abstractmethod
    def initialize(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        raise NotImplementedError

class BaseResource(IInitializableResource, ABC):

    def __init__(self, name: str):
        self._name = name
        self._is_initialized = False
        self._init_options: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._init_options = init_options

    def __str__(self) -> str:
        return self._name

class LabwareLoadable(ABC):
    @property
    def is_available(self) -> bool:
        raise NotImplementedError
    
    @property
    def labware(self) -> Optional[Labware]:
        raise NotImplementedError
    
    def set_labware(self, labware: Labware) -> None:
        # TODO: this will need to be restricted to only initilaizing the labware, probably with a LabwareManager service
        raise NotImplementedError
    
    @abstractmethod
    def prepare_for_pick(self, labware: Labware) -> None:
        raise NotImplementedError
    
    @abstractmethod
    def prepare_for_place(self, labware: Labware) -> None:
        raise NotImplementedError

    @abstractmethod
    def notify_picked(self, labware: Labware) -> None:
        raise NotImplementedError
    
    @abstractmethod
    def notify_placed(self, labware: Labware) -> None:
        raise NotImplementedError

class Equipment(BaseResource, LabwareLoadable, ABC):
    """
    Represents a piece of equipment.  Not to be used for Transporters and equipment that does not operate on labware.

    Args:
        name (str): The name of the equipment.

    Attributes:
        _command (str): The command to be executed by the equipment.
        _options (Dict[str, Any]): The options for the command.

    Methods:
        set_command: Sets the command to be executed by the equipment.
        set_command_options: Sets the options for the command.
        execute: Executes the command.

    """

    def __init__(self, name: str):
        super().__init__(name)
        self._command: Optional[str] = None
        self._options: Dict[str, Any] = {} 

    def set_command(self, command: str) -> None:
        """
        Sets the command to be executed by the equipment.

        Args:
            command (str): The command to be executed.

        Returns:
            None

        """
        self._command = command

    def set_command_options(self, options: Dict[str, Any]) -> None:
        """
        Sets the options for the command.

        Args:
            options (Dict[str, Any]): The options for the command.

        Returns:
            None

        """
        self._options = options

    def execute(self) -> None:
        """
        Executes the command.

        Raises:
            NotImplementedError: If the method is not implemented in a subclass.

        """
        raise NotImplementedError
    

    
    