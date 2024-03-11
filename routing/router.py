from typing import Any, Dict, List, Optional
from resource_models.transporter_resource import TransporterResource
from resource_models.location import Location
from resource_models.labware import Labware
from routing.system_graph import SystemGraph
from workflow_models.action import BaseAction
from workflow_models.workflow import LabwareThread


class RouteStep(BaseAction):
    def __init__(self, 
                 source: Location, 
                 target: Location, 
                 transporter: TransporterResource) -> None:
        super().__init__()
        self._source = source
        self._target = target
        self._transporter: TransporterResource = transporter
        self._post_load_actions: List[BaseAction] = []
        self._labware: Optional[Labware] = None

    @property
    def source(self) -> Location:
        return self._source
    
    @source.setter
    def source(self, source:Location) -> None:
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

        # perform the action
        for action in self._post_load_actions:
            action.execute()

    


class Route:
    def __init__(self, start: Location, end: Location) -> None:
        self._start = start
        self._end = end
        self._core_actions: Dict[str, List[BaseAction]] = {}
        self._edges: List[RouteStep] = []

    @property
    def start(self) -> Location:
        return self._start
    
    @property
    def end(self) -> Location:
        return self._end

    @property
    def path(self) -> List[Location]:
        path = [self._start]
        for edge in self._edges:
            path.append(edge.target) 
        return path
    
    @property
    def actions(self) -> List[RouteStep]:
        return self._edges

    def add_stop(self, location: Location, action: BaseAction) -> None:
        if location not in self._core_actions.keys():
            self._core_actions[location.teachpoint_name] = []
        self._core_actions[location.teachpoint_name].append(action)

    def build_route(self, system: SystemGraph) -> None:
        self._edges = []
        for core_location, core_actions in self._core_actions.items():

            # get the source and target locations
            core_src_location = self._edges[-1].target if len(self._edges) > 0 else self._start 
            core_tgt_location = system.locations[core_location]

            # get the shortest path between the two locations
            path: List[str] = system.get_shortest_available_path(core_src_location.teachpoint_name, core_tgt_location.teachpoint_name)

            # build the route actions from the path
            path_src_loc = path.pop(0)
            for path_tgt_loc in path:
                source_location = system.locations[path_src_loc]
                target_location = system.locations[path_tgt_loc]
                transporter = system.get_transporter_between(path_src_loc, path_tgt_loc)
                self._edges.append(RouteStep(source_location, target_location, transporter))
                path_src_loc = path_tgt_loc
            self._edges[-1].set_actions(core_actions)

        # build to the end of the route
        end_path: List[str] = system.get_shortest_available_path(self._edges[-1].target.teachpoint_name, self._end.teachpoint_name)
        end_path_src_loc = end_path.pop(0)
        for end_path_tgt_loc in end_path:
            source_location = system.locations[end_path_src_loc]
            target_location = system.locations[end_path_tgt_loc]
            transporter = system.get_transporter_between(end_path_src_loc, end_path_tgt_loc)
            self._edges.append(RouteStep(source_location, target_location, transporter))
            end_path_src_loc = end_path_tgt_loc                

class RouteBuilder:
    def __init__(self, thread: LabwareThread, system: SystemGraph) -> None:
        self._system = system
        self._thread = thread
        
    def _get_base_route(self) -> Route:
        route = Route(self._thread.start_location, self._thread.end_location)
        for method in self._thread.methods:
            for action in method.actions:
                route.add_stop(action.location, action)
        return route

    def get_route(self) -> Route:
        route = self._get_base_route()
        route.build_route(self._system)
        return route
