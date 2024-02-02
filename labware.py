from __future__ import annotations

from typing import List


class Labware:
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

        