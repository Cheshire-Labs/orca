from typing import Optional
from resource_models.base_resource import Equipment
from resource_models.labware import Labware


class PlatePad(Equipment):

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._can_resolve_deadlock = True
        self._is_initialized = False
        self._labware: Optional[Labware] = None

    @property
    def labware(self) -> Optional[Labware]:
        return self._labware

    def initialize(self) -> bool:
        self._is_initialized = True
        return self._is_initialized
    
    def notify_picked(self, labware: Labware) -> None:
        print(f"Labware {labware} picked from plate pad {self}")
        self._labware = None
    
    def notify_placed(self, labware: Labware) -> None:
        print(f"Labware {labware} recieved on plate pad {self}")
        self._labware = labware

    def prepare_for_pick(self, labware: Labware) -> None:
        if self._labware != labware:
            raise ValueError(f"Labware {labware} not found on plate pad {self}")
    
    def prepare_for_place(self, labware: Labware) -> None:
        if self._labware != None:
            raise ValueError(f"Labware {self._labware} already on plate pad {self}")