from __future__ import annotations

from typing import Any, Dict, Optional
import uuid

class LabwareTemplate:
    """
    Creates a template for a labware. A labware is a container that can hold samples or reagents.
    """
    def __init__(self, name: str, type: str, options: Optional[Dict[str, Any]] = None) -> None:
        """
        Initializes a LabwareTemplate instance.

        Args:
            name (str): _Name of the labware template._
            type (str): _Labware Type.  This name should match internal labware defintions of software, such as in robotic arm software._
            options (Optional[Dict[str, Any]], optional): _options to set unique properties_. Defaults to None.
        """
        self._name = name
        self._labware_type = type
        self._options = options if options is not None else {}
    
    @property
    def name(self) -> str:
        return self._name
    
    @property
    def labware_type(self) -> str:
        return self._labware_type
    
    @labware_type.setter
    def labware_type(self, value: str) -> None:
        self._labware_type = value

    def create_instance(self) -> LabwareInstance:
        return LabwareInstance(self.name, self.labware_type)

class AnyLabwareTemplate:
    """A class that represents any labware template"""

    @property
    def name(self) -> str:
        return "$any"

    def create_instance(self) -> AnyLabware:
        return AnyLabware()
    
    def __str__(self) -> str:
        return "$AnyLabwareTemplate"


class AnyLabware:
    """A class that represents any labware"""

    @property
    def name(self) -> str:
        return "$any"

    def __str__(self) -> str:
        return "$AnyLabware"


class LabwareInstance:
    
    def __init__(self, name: str, labware_type: str) -> None:
        self._id = str(uuid.uuid4())
        self._init_options: Dict[str, Any] = {}
        self._name = name
        self._labware_type = labware_type
    
    @property
    def name(self) -> str:
        return f"{self._name}"

    @property   
    def id(self) -> str:
        return self._id
    
    @property
    def labware_type(self) -> str:
        return self._labware_type

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._init_options = init_options

    def __str__(self) -> str:
        return f"{self._name}"