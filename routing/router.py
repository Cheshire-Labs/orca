from typing import Dict, List, Optional
from resource_models.base_resource import TransporterResource
from resource_models.labware import Labware
from resource_models.location import Location
from routing.system_graph import SystemGraph
from workflow_models.action import BaseAction, LoadLabwareAction, NullAction, PickAction, PlaceAction, UnloadLabwareAction
from workflow_models.workflow import LabwareThread


class RouteAction(BaseAction):
    def __init__(self, source: Location, target: Location) -> None:
        super().__init__()
        self._source = source
        self._target = target
        self._post_load_actions: List[BaseAction] = []
        self._labware: Optional[Labware] = None

    @property
    def source(self) -> Location:
        return self._source
    
    @source.setter
    def source(self, source: Location) -> None:
        self._source = source

    @property
    def target(self) -> Location:
        return self._target
    
    @target.setter
    def target(self, target: Location) -> None:
        self._target = target

    def set_actions(self, actions: List[BaseAction]) -> None:
        self._post_load_actions = actions

    def set_labware(self, labware: Labware) -> None:
        self._labware = labware

    def get_actions(self) -> List[BaseAction]:
        actions: List[BaseAction] = []
        if self._source == self._target:
            actions.extend(self._post_load_actions)
        else:
            self._append_source_actions(actions)
            self._append_target_actions(actions)
        return actions
    
    def _append_source_actions(self, actions: List[BaseAction]) -> None:
        if self._labware is None:
            raise ValueError("Labware must be set before getting actions")
        elif self._source.resource is None:
            actions.append(NullAction())
        elif self._source.resource is TransporterResource:
            # coming from robot
            actions.append(PlaceAction(self._source.resource, self._labware, self._target.name))
        else:
            actions.append(UnloadLabwareAction(self._source.resource, self._labware))

    def _append_target_actions(self, actions: List[BaseAction]) -> None:
        if self._labware is None:
            raise ValueError("Labware must be set before getting actions")
        elif self._target.resource is None:
            actions.append(NullAction())
        elif self._target.resource is TransporterResource:
            # going to robot
            actions.append(PickAction(self._target.resource, self._labware, self._source.name))
        else:
            actions.append(LoadLabwareAction(self._target.resource, self._labware))
            actions.extend(self._post_load_actions)

    
    def _perform_action(self) -> None:
        for action in self.get_actions():
            action.execute() 
        



class Route:
    def __init__(self, start: Location, end: Location) -> None:
        self._start = start
        self._end = end
        self._core_actions: Dict[str, List[BaseAction]] = {}
        self._edges: List[RouteAction] = []

    @property
    def path(self) -> List[Location]:
        path = [self._start]
        for edge in self._edges:
            path.append(edge.target) 
        return path

    def add_stop(self, location: str, action: BaseAction) -> None:
        if location not in self._core_actions.keys():
            self._core_actions[location] = []
        self._core_actions[location].append(action)

    def _build_route(self, system: SystemGraph) -> None:
        self._edges = []
        for core_location, core_actions in self._core_actions.items():

            # get the source and target locations
            core_src_location = self._start if len(self._edges) > 0 else self._edges[-1].target
            core_tgt_location = system.locations[core_location]

            # get the shortest path between the two locations
            path: List[str] = system.get_shortest_available_path(core_src_location.name, core_tgt_location.name)

            # build the route actions from the path
            path_src_loc = path.pop(0)
            for path_tgt_loc in path:
                source_location = system.locations[path_src_loc]
                target_location = system.locations[path_tgt_loc]
                self._edges.append(RouteAction(source_location, target_location))
                path_src_loc = path_tgt_loc
            self._edges[-1].set_actions(core_actions)                

class RouteBuilder:
    def __init__(self, thread: LabwareThread, system: SystemGraph) -> None:
        self._system = system
        self._thread = thread
        
    def _get_base_route(self) -> Route:
        route = Route(self._thread.start_location, self._thread.end_location)
        for method in self._thread.methods:
            for action in method.actions:
                res_location = self._system.get_resource_location(action.resource.name)
                route.add_stop(res_location, action)
        return route

    def get_route(self) -> Route:
        route = self._get_base_route()
        route._build_route(self._system)
        return route
