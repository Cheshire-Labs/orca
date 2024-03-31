from typing import Any, Dict, List, Optional
from resource_models.labware import Labware
from resource_models.location import Location
from resource_models.transporter_resource import TransporterResource
from system.system_map import SystemMap
from workflow_models.action import BaseAction


class MoveAction(BaseAction):
    def __init__(self,
                 source: Location,
                 target: Location,
                 transporter: TransporterResource) -> None:
        super().__init__()
        self._source = source
        self._target = target
        self._transporter: TransporterResource = transporter

    @property
    def source(self) -> Location:
        return self._source

    @property
    def target(self) -> Location:
        return self._target
    
    def set_labware(self, labware: Labware) -> None:
        self._labware = labware
    
    def _perform_action(self) -> None:
        if self._labware is None:
            raise ValueError("Labware must be set before getting actions")

        # move the labware
        self._source.prepare_for_pick(self._labware)
        self._transporter.pick(self._source)
        self._source.notify_picked(self._labware)
        self._target.prepare_for_place(self._labware)
        self._transporter.place(self._target)
        self._target.notify_placed(self._labware)

class Route:
    def __init__(self, start: Location, system_map: SystemMap) -> None:
        self._start = start
        self._end = start
        self._path: List[Location] = [start]
        self._system_map = system_map
        # self._core_actions: Dict[str, List[ResourceAction]] = {}
        
    @property
    def start(self) -> Location:
        return self._start

    @property
    def path(self) -> List[Location]:
        return self._path

    def extend_to_location(self, end_location: Location) -> None:
    
        path = self._system_map.get_shortest_available_path(self._end.teachpoint_name, end_location.teachpoint_name)
        for stop in path[1:]:
            self._path.append(self._system_map.get_location(stop))
        self._end = end_location

    def __iter__(self):
        return iter(self._path)
    
    def __getitem__(self, index: int) -> Location:
        return self._path[index]
 