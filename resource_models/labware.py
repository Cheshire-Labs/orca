from __future__ import annotations

from typing import Any, Dict
import uuid

class LabwareTemplate:
    def __init__(self, name: str, type: str):
        self._name = name
        self._labware_type = type
    
    @property
    def name(self) -> str:
        return self._name
    
    @property
    def labware_type(self) -> str:
        return self._labware_type
    
    @labware_type.setter
    def labware_type(self, value: str) -> None:
        self._labware_type = value

class Labware:

    @staticmethod
    def from_template(labware: LabwareTemplate) -> Labware:
        return Labware(labware.name, labware.labware_type)
    
    def __init__(self, name: str, labware_type: str) -> None:
        self._id = str(uuid.uuid4())
        self._init_options: Dict[str, Any] = {}
        self._name = name
        self._labware_type = labware_type
    
    @property
    def name(self) -> str:
        return f"{self._name}:{self._id}"

    @property   
    def id(self) -> str:
        return self._id
    
    @property
    def labware_type(self) -> str:
        return self._labware_type

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._init_options = init_options

    def __str__(self) -> str:
        return f"{self._name}:{self._id}"