from typing import Any, Dict, List, Optional
from labware import Labware
from location import Location
from method import Method, MethodStatus


class LabwareThread:
    @property
    def labware(self) -> Labware:
        return self._labware
    
    @property
    def start(self) -> Location:
        return self._start
    
    @property
    def end(self) -> Location:
        return self._end
    
    @property
    def methods(self) -> List[Method]:
        return self._methods
    
    
    def __init__(self, labware: Labware, start: Location, end: Location) -> None:
        self._labware: Labware = labware
        self._start: Location = start
        self._end: Location = end
        self._methods: List[Method] = []

    def add_method(self, method: Method, method_step_options: Optional[Dict[str, Any]] = None) -> None:
        # TODO: may add option to update method options at the Workflow level
        self._methods.append(method)

    def get_current_method(self) -> Optional[Method]:
        return next((step for step in self._methods if step.status in [MethodStatus.RUNNING]), None)

    def get_next_method(self) -> Optional[Method]:
        return next((step for step in self._methods if step.status in [MethodStatus.QUEUED]), None)
    

class Workflow:
    @property
    def labware_threads(self) -> Dict[str, LabwareThread]:
        return self._labware_thread

    def __init__(self, name: str) -> None:
        self._name = name
        self._labware_thread: Dict[str, LabwareThread] = {}

    def set_status_queued(self) -> None:
        for thread in self._labware_thread.values():
            for method in thread.methods:
                for action in method.actions:
                    action.set_status(MethodStatus.QUEUED)